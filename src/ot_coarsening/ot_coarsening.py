##Method2: Directly compare original features and coarsed features (with the same dimension,
# GNN(fe, hidden) - Coarsen - GNN(hidden, fe) ).
import torch
import torch.nn.functional as F
from torch.nn import Linear
import torch_geometric.utils as U
import torch_geometric.transforms as T
from torch_geometric.data import Data, Batch
from torch_geometric.nn import DenseSAGEConv, DenseGCNConv, JumpingKnowledge, GCNConv
from sinkhorn import sinkhorn_loss_default
from pytorch_memlab import profile, set_target_gpu, profile_every
import numpy as np

class GNNBlock(torch.nn.Module): #2 layer GCN block
    def __init__(self, in_channels, hidden_channels, out_channels):
        super(GNNBlock, self).__init__()

        self.conv1 = DenseGCNConv(in_channels, hidden_channels)
        self.conv2 = DenseGCNConv(hidden_channels, out_channels)

        self.lin = torch.nn.Linear(hidden_channels + out_channels,
                                   out_channels)
        #self.lin1 = torch.nn.Linear(hidden_channels, out_channels)

    def reset_parameters(self):
        self.conv1.reset_parameters()
        self.conv2.reset_parameters()
        self.lin.reset_parameters()

    def forward(self, x, adj, mask=None, add_loop=True):
        x1 = F.relu(self.conv1(x, adj, mask, add_loop))
        x2 = F.relu(self.conv2(x1, adj, mask, add_loop))
        return self.lin(torch.cat([x1, x2], dim=-1))
        # return self.lin1(x1, dim=-1)

class CoarsenBlock(torch.nn.Module):
    def __init__(self, in_channels, assign_ratio, max_number_of_nodes=1000):
        super(CoarsenBlock, self).__init__()
        self.gcn_att = GCNConv(in_channels, 1, bias=True)
        self.assign_ratio = assign_ratio
        self.max_number_of_nodes = max_number_of_nodes

    def reset_parameters(self):
        self.gcn_att.reset_parameters()

    def get_topk_range_for_sentences(self, alpha_vec, output_node_count, num_sentences):
        assert num_sentences >= output_node_count
        alpha_vec = alpha_vec.detach().cpu().numpy()
        full_argsort = np.argsort(alpha_vec)[::-1]
        topk_ind = [] 
        num_sentences = 0
        for index in full_argsort:
            if num_sentences == output_node_count:
                break
            is_sentence = index < num_sentences 
            if is_sentence:
                num_sentences += 1
            topk_ind.append(index)
        
        temptopk = torch.Tensor(alpha_vec[topk_ind])
        topk_ind = torch.Tensor(topk_ind.copy()).int()
        # return temptopk and topkind 
        return temptopk, topk_ind
    
    def calculate_attention(self, data):
        # unpack data
        x = data.x
        edge_index = data.edge_index
        edge_attr = data.edge_attr
        edge_weight = data.edge_attr.squeeze().float()
        # compute max num nodes
        _, mask = U.to_dense_batch(x, batch=data.batch)
        batch_num_nodes = mask.sum(-1)
        max_num_nodes = torch.max(batch_num_nodes)
        # compute attention
        alpha_vec = self.gcn_att(x, edge_index, edge_weight)
        alpha_vec = torch.pow(alpha_vec, 2)
        alpha_vec = F.sigmoid(alpha_vec).squeeze() # b*n*1 --> b*n
        # reshape alpha_vec
        output_alpha_vecs = []
        index = 0
        for num_nodes in batch_num_nodes:
            section_vector = alpha_vec[index: index + num_nodes]
            vector_shape = section_vector.shape[0]
            section_vector = F.pad(section_vector, (0, max_num_nodes - vector_shape))
            output_alpha_vecs.append(section_vector)
            index += num_nodes
        
        output_alpha_vecs = torch.stack(output_alpha_vecs)
        return output_alpha_vecs

    def convert_graph_to_dense(self, x, edge_index, edge_attr):
        graph = Data(x=x, edge_index=edge_index, edge_attr=edge_attr)
        adj = U.to_dense_adj(graph.edge_index, edge_attr=graph.edge_attr)
        new_graph = T.ToDense()(graph)
        graph.adj = adj
        assert torch.eq(new_graph.adj, adj)
        return graph

    def normalize_batch_adj(self, adj):  # adj shape: batch_size * num_node * num_node, D^{-1/2} (A+I) D^{-1/2}
        dim = adj.size()[1]
        A = adj + torch.eye(dim, device=adj.device)
        deg_inv_sqrt = A.sum(dim=-1).clamp(min=1).pow(-0.5)
        newA = deg_inv_sqrt.unsqueeze(-1) * A * deg_inv_sqrt.unsqueeze(-2)
        newA = (adj.sum(-1)>0).float().unsqueeze(-1).to(adj.device) * newA
        return newA
    
    def convert_sparse_to_dense(self, graph):
        #edge_index = graph.edge_index
        #edge_attr = graph.edge_attr
        #adj = U.to_dense_adj(edge_index, edge_attr=edge_attr) 
        graph = T.ToDense()(graph)
        #graph.adj = graph.adj.squeeze()
        #graph.adj = adj
        return graph
    
    def convert_dense_to_sparse(self, graph):
        adj = graph.adj
        edge_index, edge_attr = U.dense_to_sparse(adj)
        graph.edge_index = edge_index
        graph.edge_attr = edge_attr 
        return graph
    
    def single_graph_forward(self, alpha_vec, graph, output_node_count=None, num_sentences=None):
        """
            Performs forward pass for a single graph
        """
        # convert the graph to dense
        dense_graph = self.convert_sparse_to_dense(graph)
        adj = dense_graph.adj.squeeze().float()
        #adj = adj.type(torch.float64)
        x = dense_graph.x
        #x = x.type(torch.float64)
        num_nodes = x.shape[0]
        alpha_vec = alpha_vec[0:num_nodes]
        # normalize adjacency matrix
        norm_adj = self.normalize_batch_adj(adj.unsqueeze(0)).squeeze()
        # get topk
        cut_value = 0
        if num_sentences is None:
            # This is in the train phase
            num_coarse_nodes = int(num_nodes * self.assign_ratio) + 1
            temptopk, topk_ind = alpha_vec.topk(num_coarse_nodes, dim=-1)
            cut_value = temptopk[-1]
        else:
            # This is in the evaluation phase
            temptopk, topk_ind = self.get_topk_range_for_sentences(alpha_vec, output_node_count, num_sentences)
            cut_value = temptopk[-1]
        # calculate S
        cut_alpha_vec = F.relu(alpha_vec + 0.0000001 - cut_value)
        repeated_cut_alpha_vec = cut_alpha_vec.repeat(cut_alpha_vec.shape[0], 1)
        #print("repeated cut alpha vec shape")
        #print(repeated_cut_alpha_vec.shape)
        #print("Repeated cut alpha vec nonzero")
        #print(torch.nonzero(repeated_cut_alpha_vec).shape)
        S = norm_adj * repeated_cut_alpha_vec
        S = F.normalize(S, p=1, dim=-1)
        #S = S.type(torch.float64)
        #print("num nonzero s")
        #print(torch.nonzero(S).shape)
        #print("Num nonzero adj")
        #print(torch.nonzero(adj).shape)
        #print(S)
        # perform the graph coarsening
        coarse_x = torch.matmul(torch.transpose(S, 0, 1), x)  
        #print("sorted flat adjacency matrix")
        flat_adj = torch.flatten(adj)
        #print(flat_adj[list(torch.nonzero(flat_adj))].sort()[::-1])
        coarse_adj = torch.matmul(torch.matmul(torch.transpose(S, 0, 1), adj), S)  # batched matrix multiply
        n_digits = 4
        coarse_adj = torch.floor(coarse_adj * 10**n_digits) / (10**n_digits)
        #print("Coarse adjacency matrix nonzero")
        #print(torch.nonzero(coarse_adj).shape)    
        flat_adj = torch.flatten(coarse_adj)
        #print("sorted flat coarse adjacency matrix")
        #print(flat_adj[list(torch.nonzero(flat_adj))].sort()[::-1])
        # convert dense graph back to sparse
        dense_coarse_graph = Data(adj=coarse_adj, x=coarse_x)
        sparse_graph = self.convert_dense_to_sparse(dense_coarse_graph)
        del sparse_graph.adj

        return sparse_graph, S, topk_ind

    def forward(self, data, num_sentences=None, output_node_counts=None):
        # compute attention scores
        alpha_vec = self.calculate_attention(data)
        # convert batch to list
        graph_list = data.to_data_list()
        batch_size = len(graph_list)
        # go through the batch and coarsen each of the graphs
        batch_topk_ind = []
        sparse_graphs = []
        Ss = []
        for j in range(batch_size):
            # prepare data for individual forward pass
            if output_node_counts is None:
                output_node_count = None
                current_num_sentences = None
            else:
                output_node_count = output_node_counts[j].int().item()
                current_num_sentences = num_sentences[j]
            current_graph = graph_list[j]
            # prepare alpha vec 
            current_alpha_vec = alpha_vec[j]
            # run forward pass for this single graph
            sparse_graph, S, topk_ind = self.single_graph_forward(current_alpha_vec,
                                                                     current_graph, 
                                                                     output_node_count=output_node_count, 
                                                                     num_sentences=current_num_sentences)
            # add to lists
            Ss.append(S)
            sparse_graphs.append(sparse_graph)
            batch_topk_ind.append(topk_ind)
        # convert lists to batched input format
        sparse_batch = Batch.from_data_list(sparse_graphs)         
        return sparse_batch, Ss, batch_topk_ind

class Coarsening(torch.nn.Module):
    def __init__(self, dataset, hidden, ratio=0.5, epsilon=1.0, opt_epochs=10): # we only use 1 layer for coarsening
        super(Coarsening, self).__init__()
        self.ratio = ratio
        self.epsilon = epsilon
        self.opt_epochs = opt_epochs
        # self.embed_block1 = GNNBlock(dataset.num_features, hidden, hidden)
        self.embed_block1 = GCNConv(dataset.num_features, hidden)
        self.coarse_block1 = CoarsenBlock(hidden, ratio)
        self.embed_block2 = GCNConv(hidden, dataset.num_features)

    def reset_parameters(self):
        self.embed_block1.reset_parameters()
        self.coarse_block1.reset_parameters()

    def compute_loss(self, data, coarse_data, epsilon, opt_epochs, p):
        opt_loss = 0.0
        data_list = data.to_data_list()
        coarse_list = coarse_data.to_data_list()
        for i in range(data.num_graphs):
            x = data_list[i].x
            x2 = coarse_list[i].x
            x3 = self.get_nonzero_rows(x)
            print("x3 x2 shapes")
            print(x3.shape)
            print(x2.shape)
            opt_loss += sinkhorn_loss_default(x3, x2, epsilon, niter=opt_epochs, p=p)

        return opt_loss / data.num_graphs   

    def batch_copy(self, batch):
        """
            Does a deep copy of the batch
        """ 
        batch_list = batch.to_data_list()
        new_batch_list = [] 
        for data in batch_list:
            data_copy = data.clone()
            new_batch_list.append(data_copy)

        new_batch = Batch.from_data_list(new_batch_list)
        return new_batch

    #@profile_every(1)
    def forward(self, data, p=2, output_node_counts=None, num_sentences=None):
        data_copy = self.batch_copy(data)
        x, edge_index, edge_attr = data.x, data.edge_index, data.edge_attr
        batch_num_nodes = x.shape[0]
        # convert edge_attr to edge_weight
        edge_weight = edge_attr.squeeze().float()
        edge_index = edge_index.long()
        x = x.float()
        #print("start x shape")
        #print(x.shape)
        #print("start edge index shape")
        #print(edge_index.shape)
        #print("start edge weight shape")
        #print(edge_weight.shape)
        x1 = self.embed_block1(x, edge_index, edge_weight)
        x1 = F.relu(x1)
        # make batched data object
        data1 = data
        data1.x = x1
        # convert the data to dense for this phase
        coarse_batch, S, batch_topk_ind = self.coarse_block1(data1,
                                                                  num_sentences=num_sentences,
                                                                  output_node_counts=output_node_counts)
        #print(coarse_batch)
        coarse_x = coarse_batch.x
        #print("end coarse_x shape")
        #print(coarse_x.shape)
        xs = [coarse_x.mean(dim=1)]
        coarse_edge_index = coarse_batch.edge_index
        #print("end coarse edge index")
        #print(coarse_edge_index.shape)
        coarse_edge_attr = coarse_batch.edge_attr
        #print("end coarse edge attr")
        #print(coarse_edge_attr.shape)
        coarse_edge_weight = coarse_edge_attr.squeeze().float()
        x2 = F.tanh(self.embed_block2(coarse_x, coarse_edge_index, coarse_edge_weight))
        coarse_batch.x = x2
        #print(x2.shape)
        xs.append(x2.mean(dim=1))
        opt_loss = self.compute_loss(data_copy, coarse_batch, self.epsilon, self.opt_epochs, p)

        return xs, edge_index, edge_attr, S, opt_loss, batch_topk_ind

    def get_nonzero_rows(self, M):
        # M is a matrix
        #row_ind = M.sum(-1).nonzero().squeeze() #nonzero has bugs in Pytorch 1.2.0.........
        #return row_ind
        #So we use other methods to take place of it
        MM, MM_ind = M.sum(-1).sort()
        N = (M.sum(-1)>0).sum()
        return M[MM_ind[:N]]

    def __repr__(self):
        return self.__class__.__name__

class MultiLayerCoarsening(torch.nn.Module):
    def __init__(self, dataset, hidden, num_layers=2, ratio=0.5):
        super(MultiLayerCoarsening, self).__init__()
        self.embed_block1 = DenseGCNConv(dataset.num_features, hidden)
        self.coarse_block1 = CoarsenBlock(hidden, ratio)
        self.embed_block2 = DenseGCNConv(hidden, dataset.num_features)
        # self.embed_block2 = GNNBlock(hidden, hidden, dataset.num_features)
        self.num_layers = num_layers
        self.jump = JumpingKnowledge(mode='cat')
        # self.lin1 = Linear( hidden + dataset.num_features, hidden)
        # self.lin2 = Linear(hidden, dataset.num_classes)

    def reset_parameters(self):
        self.embed_block1.reset_parameters()
        self.coarse_block1.reset_parameters()
        self.embed_block2.reset_parameters()

        self.jump.reset_parameters()
        # self.lin1.reset_parameters()

    """
        Compute loss for each example
    """
    def compute_loss(self, x, x2, epsilon, out_epochs):
        opt_loss = 0.0
        for i in range(len(x)):
            x3 = self.get_nonzero_rows(x[i])
            x4 = self.get_nonzero_rows(x2[i])
            if x3.size()[0]==0:
                continue
            if x4.size()[0]==0:
                # opt_loss += sinkhorn_loss_default(x3, x2[i], epsilon, niter=opt_epochs).float()
                continue
            opt_loss += sinkhorn_loss_default(x3, x4, epsilon, niter=opt_epochs).float()
        
        return opt_loss / len(x)
    
    def get_original_graph_coarse_indices(self, batch_topks):
        """
            Returns a tensor with each index corresponding to the
            a node in the coarse graph. The value at the index is the
            index of the original node corresponding to the coarse node. 
            
            parameters
                - batch_topks: This is a list with length "num layers" with each element "batch_size" where
                 each element contains a list of indices corresponding to the nodes 
                in the coarse graph. The value at each index is the node in the original 
                graph the coarse node maps to. 
                
                Example: topk_ind[i] = j means node i in the coarse graph maps to node j in 
                the original graph.  
        """
        # shape is (num_layers, batch_size, num_coarse_nodes)  
        # transpose num_layers and batch_size 
        batch_topks = [list(i) for i in zip(*batch_topks)] 
        batch_coarse_indices = []
        # shape is (batch_size, num_layers, num_coarse_nodes)
        for batch_index, layerwise_topks in enumerate(batch_topks):
            coarse_indices = layerwise_topks[-1]
            for layer_num in reversed(range(len(layerwise_topks) - 1)):
                topks = layerwise_topks[layer_num]
                coarse_indices = [topks[coarse_index] for coarse_index in coarse_indices]
            # should be the same size as the original coarse_indices with indices
            # domain of the original graph
            batch_coarse_indices.append(coarse_indices)
    
        return batch_coarse_indices

    def forward(self, data, epsilon=0.01, opt_epochs=100):
        x, adj, mask = data.x, data.adj, data.mask
        batch_num_nodes = data.mask.sum(-1)
        batch_topks = []
        new_adjs = [adj]
        Ss = []
        x1 = F.relu(self.embed_block1(x, adj, mask, add_loop=True))
        xs = [x1.mean(dim=1)]
        new_adj = adj
        coarse_x = x1

        for i in range(self.num_layers):
            coarse_x, new_adj, S, batch_topk_ind = self.coarse_block1(coarse_x, new_adj, batch_num_nodes)
            new_adjs.append(new_adj)
            Ss.append(S)
            batch_topks.append(batch_topk_ind)

        x2 = self.embed_block2(coarse_x, new_adj, mask, add_loop=True) #should not add ReLu, otherwise x2 could be all zero.
        xs.append(x2.mean(dim=1))
        # compute loss 
        opt_loss = self.compute_loss(x, x2, epsilon, out_epochs)
        coarse_indices = self.get_original_graph_coarse_indices(batch_topks)
        return xs, new_adjs, Ss, opt_loss, coarse_indices

    def get_nonzero_rows(self, M):# M is a matrix
        # row_ind = M.sum(-1).nonzero().squeeze() #nonzero has bugs in Pytorch 1.2.0.........
        # So we use other methods to take place of it
        MM, MM_ind = torch.abs(M.sum(-1)).sort()
        N = (torch.abs(M.sum(-1)) > 0).sum()
        return M[MM_ind[:N]]

    def __repr__(self):
        return self.__class__.__name__
