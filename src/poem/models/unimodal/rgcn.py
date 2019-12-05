# -*- coding: utf-8 -*-

"""Implementation of the R-GCN model."""

import logging
from os import path
from typing import Any, Mapping, Optional, Type

import torch
from torch import nn
from torch.nn import functional

from . import ComplEx, DistMult, ERMLP
from ..base import BaseModule
from ...losses import Loss
from ...triples import TriplesFactory
from ...typing import InteractionFunction

__all__ = [
    'RGCN',
]

logger = logging.getLogger(name=path.basename(__file__))


def _get_neighborhood(
    start_nodes: torch.LongTensor,
    sources: torch.LongTensor,
    targets: torch.LongTensor,
    k: int,
    num_nodes: int,
    undirected: bool = False,
) -> torch.BoolTensor:
    # Construct node neighbourhood mask
    node_mask = torch.zeros(num_nodes, device=start_nodes.device, dtype=torch.bool)

    # Set nodes in batch to true
    node_mask[start_nodes] = True

    # Compute k-neighbourhood
    for _ in range(k):
        # if the target node needs an embeddings, so does the source node
        node_mask[sources] |= node_mask[targets]

        if undirected:
            node_mask[targets] |= node_mask[sources]

    # Create edge mask
    edge_mask = node_mask[targets]

    if undirected:
        edge_mask |= node_mask[sources]

    return edge_mask


class RGCN(BaseModule):
    """An implementation of R-GCN from [schlichtkrull2018]_.

    This model uses graph convolutions with relation-specific weights.

    .. seealso::

       - `Pytorch Geometric's implementation of R-GCN
         <https://github.com/rusty1s/pytorch_geometric/blob/1.3.2/examples/rgcn.py>`_
       - `DGL's implementation of R-GCN
         <https://github.com/dmlc/dgl/tree/v0.4.0/examples/pytorch/rgcn>`_
    """

    #: Interaction model used as decoder
    base_model: BaseModule

    #: The blocks of the relation-specific weight matrices
    #: shape: (num_relations, num_blocks, embedding_dim//num_blocks, embedding_dim//num_blocks)
    blocks: Optional[nn.ParameterList]

    #: The base weight matrices to generate relation-specific weights
    #: shape: (num_bases, embedding_dim, embedding_dim)
    bases: Optional[nn.ParameterList]

    #: The relation-specific weights for each base
    #: shape: (num_relations, num_bases)
    att: Optional[nn.ParameterList]

    #: The biases for each layer (if used)
    #: shape of each element: (embedding_dim,)
    biases: Optional[nn.ParameterList]

    #: Batch normalization for each layer (if used)
    batch_norms: Optional[nn.ModuleList]

    #: Activations for each layer (if used)
    activations: Optional[nn.ModuleList]

    hpo_default = dict(
        embedding_dim=dict(type=int, low=50, high=1000, q=50),
        num_bases_or_blocks=dict(type=int, low=2, high=20, q=1),
        num_layers=dict(type=int, low=1, high=5, q=1),
        use_bias=dict(type='bool'),
        use_batch_norm=dict(type='bool'),
        activation_cls=dict(type='categorical', choices=[None, nn.ReLU, nn.LeakyReLU]),
        base_model_cls=dict(type='categorical', choices=[DistMult, ComplEx, ERMLP]),
        edge_dropout=dict(type=float, low=0.0, high=.9),
        self_loop_dropout=dict(type=float, low=0.0, high=.9),
        message_normalization=dict(type='categorical', choices=[None, 'nonsymmetric', 'symmetric']),
        decomposition=dict(type='categorical', choices=['basis', 'block']),
    )

    def __init__(
        self,
        triples_factory: TriplesFactory,
        embedding_dim: int = 500,
        entity_embeddings: Optional[nn.Embedding] = None,
        criterion: Optional[Loss] = None,
        predict_with_sigmoid: bool = False,
        preferred_device: Optional[str] = None,
        random_seed: Optional[int] = None,
        num_bases_or_blocks: int = 5,
        num_layers: int = 2,
        use_bias: bool = True,
        use_batch_norm: bool = False,
        activation_cls: Optional[Type[nn.Module]] = None,
        activation_kwargs: Optional[Mapping[str, Any]] = None,
        base_model_cls: Optional[Type[BaseModule]] = None,
        sparse_messages_owa: bool = True,
        edge_dropout: float = 0.4,
        self_loop_dropout: float = 0.2,
        message_normalization: str = 'nonsymmetric',
        decomposition: str = 'basis',
        buffer_messages: bool = True,
    ):
        if base_model_cls is None:
            base_model_cls = DistMult

        if activation_cls is None:
            activation_cls = nn.ReLU

        # Instantiate model
        base_model = base_model_cls(
            triples_factory=triples_factory,
            embedding_dim=embedding_dim,
            entity_embeddings=entity_embeddings,
            random_seed=random_seed,
        )

        super().__init__(
            triples_factory=triples_factory,
            embedding_dim=embedding_dim,
            entity_embeddings=base_model.entity_embeddings,
            criterion=criterion,
            predict_with_sigmoid=predict_with_sigmoid,
            preferred_device=preferred_device,
            random_seed=random_seed,
        )

        if self.triples_factory.create_inverse_triples:
            raise ValueError('R-GCN handles edges in an undirected manner.')

        # Base model has to be set **after** nn.Module.__init__
        self.base_model = base_model

        self.decomposition = decomposition
        # Heuristic
        if self.decomposition == 'basis':
            if num_bases_or_blocks is None:
                logging.info('Using a heuristic to determine the number of bases.')
                num_bases_or_blocks = triples_factory.num_relations // 2 + 1
            if num_bases_or_blocks > triples_factory.num_relations:
                raise ValueError('The number of bases should not exceed the number of relations.')
        elif self.decomposition == 'block':
            if num_bases_or_blocks is None:
                logging.info('Using a heuristic to determine the number of blocks.')
                num_bases_or_blocks = 2
            if embedding_dim % num_bases_or_blocks != 0:
                raise ValueError(
                    'With block decomposition, the embedding dimension has to be divisible by the number of'
                    f' blocks, but {embedding_dim} % {num_bases_or_blocks} != 0.'
                )
        else:
            raise ValueError(f'Unknown decomposition: "{decomposition}". Please use either "basis" or "block".')

        self.num_bases = num_bases_or_blocks

        # buffering of messages
        self.buffer_messages = buffer_messages
        self.enriched_embeddings = None

        # TODO: Better use a enum for that?
        allowed_normalizations = {None, 'symmetric', 'nonsymmetric'}
        if message_normalization not in allowed_normalizations:
            raise ValueError(
                f'Unknown message normalization: "{message_normalization}". Please use one of {allowed_normalizations}.'
            )

        self.message_normalization = message_normalization
        self.edge_dropout = edge_dropout
        if self_loop_dropout is None:
            self_loop_dropout = edge_dropout
        self.self_loop_dropout = self_loop_dropout
        self.use_batch_norm = use_batch_norm
        self.activation_cls = activation_cls
        self.activation_kwargs = activation_kwargs
        if use_batch_norm:
            if use_bias:
                logger.warning('Disabling bias because batch normalization was used.')
            use_bias = False
        self.use_bias = use_bias
        self.num_layers = num_layers
        self.sparse_messages_owa = sparse_messages_owa

        # Save graph using buffers, such that the tensors are moved together with the model
        h, r, t = self.triples_factory.mapped_triples.t()
        self.register_buffer('sources', h)
        self.register_buffer('targets', t)
        self.register_buffer('edge_types', r)

        # Weights
        self.bases = None
        self.att = None
        self.biases = None
        self.batch_norms = None
        self.activations = None

        # Finalize initialization
        self._init_weights_on_device()

    def post_parameter_update(self) -> None:  # noqa: D102
        super().post_parameter_update()

        # invalidate enriched embeddings
        self.enriched_embeddings = None

    def init_empty_weights_(self):  # noqa: D102
        self.base_model = self.base_model.init_empty_weights_()
        if self.decomposition == 'basis':
            if self.bases is None:
                self.bases = nn.ParameterList()
                for _ in range(self.num_layers):
                    layer_bases_init = torch.empty(
                        self.num_bases,
                        self.embedding_dim,
                        self.embedding_dim,
                        device=self.device,
                    )
                    gain = nn.init.calculate_gain(nonlinearity=self.activation_cls.__name__.lower())
                    nn.init.xavier_normal_(layer_bases_init, gain=gain)
                    layer_bases = nn.Parameter(layer_bases_init, requires_grad=True)
                    self.bases.append(layer_bases)
            if self.att is None:
                self.att = nn.ParameterList()
                for _ in range(self.num_layers):
                    # Random convex-combination of bases for initialization (guarantees that initial weight matrices are
                    # initialized properly)
                    # We have one additional relation for self-loops
                    att_init = torch.rand(self.num_relations + 1, self.num_bases, device=self.device)
                    functional.normalize(att_init, p=1, dim=1, out=att_init)
                    att = nn.Parameter(att_init, requires_grad=True)
                    self.att.append(att)
        elif self.decomposition == 'block':
            if self.bases is None:
                self.bases = nn.ParameterList()
                block_size = self.embedding_dim // self.num_bases
                for _ in range(self.num_layers):
                    layer_bases_init = torch.empty(
                        self.num_relations + 1,
                        self.num_bases,
                        block_size,
                        block_size,
                        device=self.device,
                    )
                    gain = nn.init.calculate_gain(nonlinearity=self.activation_cls.__name__.lower())
                    # Xavier Glorot initialization of each block
                    std = torch.sqrt(torch.as_tensor(2.)) * gain / (2 * block_size)
                    nn.init.normal_(layer_bases_init, std=std)
                    layer_bases = nn.Parameter(layer_bases_init, requires_grad=True)
                    self.bases.append(layer_bases)

        if self.biases is None and self.use_bias:
            self.biases = nn.ParameterList([
                nn.Parameter(torch.zeros(self.embedding_dim, device=self.device), requires_grad=True)
                for _ in range(self.num_layers)
            ])
        if self.batch_norms is None and self.use_batch_norm:
            self.batch_norms = nn.ModuleList([
                nn.BatchNorm1d(num_features=self.embedding_dim)
            ])
        if self.activation_cls is not None and self.activations is None:
            self.activations = nn.ModuleList([
                self.activation_cls(**(self.activation_kwargs or {})) for _ in range(self.num_layers)
            ])
        return self

    def clear_weights_(self):  # noqa: D102
        self.bases = None
        self.att = None
        self.biases = None
        self.batch_norms = None
        return self

    @property
    def _entity_embeddings(self) -> nn.Embedding:
        """Shorthand for the entity embeddings."""
        return self.base_model.entity_embeddings

    @property
    def _relation_embeddings(self) -> nn.Embedding:
        """Shorthand for the relation embeddings."""
        return self.base_model.relation_embeddings

    @property
    def _interaction_function(self) -> InteractionFunction:
        """Shorthand for the interaction function."""
        return self.base_model.interaction_function

    def _enrich_embeddings(self, batch: Optional[torch.LongTensor] = None) -> torch.FloatTensor:
        """
        Enrich the entity embeddings using R-GCN message propagation.

        :return: shape: (num_entities, embedding_dim)
            The updated entity embeddings
        """
        # use buffered messages if applicable
        if batch is None and self.enriched_embeddings is not None:
            return self.enriched_embeddings

        # Bind fields
        # shape: (num_entities, embedding_dim)
        x = self._entity_embeddings.weight
        sources = self.sources
        targets = self.targets
        edge_types = self.edge_types

        # Edge dropout: drop the same edges on all layers (only in training mode)
        if self.training and self.edge_dropout is not None:
            # Get random dropout mask
            edge_keep_mask = torch.rand(self.sources.shape[0], device=x.device) > self.edge_dropout

            # Apply to edges
            sources = sources[edge_keep_mask]
            targets = targets[edge_keep_mask]
            edge_types = edge_types[edge_keep_mask]

        # Different dropout for self-loops (only in training mode)
        if self.training and self.self_loop_dropout is not None:
            node_keep_mask = torch.rand(self.num_entities, device=x.device) > self.self_loop_dropout
        else:
            node_keep_mask = None

        # If batch is given, compute (num_layers)-hop neighbourhood
        if batch is not None:
            start_nodes = torch.cat([batch[:, 0], batch[:, 2]], dim=0)
            edge_mask = _get_neighborhood(
                start_nodes=start_nodes,
                sources=sources,
                targets=targets,
                k=self.num_layers,
                num_nodes=self.num_entities,
                undirected=True,
            )
        else:
            edge_mask = None

        for i in range(self.num_layers):
            # Initialize embeddings in the next layer for all nodes
            new_x = torch.zeros_like(x)

            # TODO: Can we vectorize this loop?
            for r in range(self.num_relations):
                # Choose the edges which are of the specific relation
                mask = (edge_types == r)

                # Only propagate messages on subset of edges
                if edge_mask is not None:
                    mask &= edge_mask

                # No edges available? Skip rest of inner loop
                if not mask.any():
                    continue

                # Get source and target node indices
                sources_r = sources[mask]
                targets_r = targets[mask]

                # send messages in both directions
                sources_r, targets_r = torch.cat([sources_r, targets_r]), torch.cat([targets_r, sources_r])

                # Select source node embeddings
                x_s = x[sources_r]

                # get relation weights
                w = self._get_relation_weights(i_layer=i, r=r)

                # Compute message (b x d) * (d x d) = (b x d)
                m_r = x_s @ w

                # Normalize messages by relation-specific in-degree
                if self.message_normalization == 'nonsymmetric':
                    # Calculate in-degree, i.e. number of incoming edges of relation type r
                    uniq, inv, cnt = torch.unique(targets_r, return_counts=True, return_inverse=True)
                    m_r /= cnt[inv].unsqueeze(dim=1).float()
                elif self.message_normalization == 'symmetric':
                    # Calculate in-degree, i.e. number of incoming edges of relation type r
                    uniq, inv, cnt = torch.unique(targets_r, return_counts=True, return_inverse=True)
                    m_r /= cnt[inv].unsqueeze(dim=1).float().sqrt()

                    # Calculate out-degree, i.e. number of outgoing edges of relation type r
                    uniq, inv, cnt = torch.unique(sources_r, return_counts=True, return_inverse=True)
                    m_r /= cnt[inv].unsqueeze(dim=1).float().sqrt()
                else:
                    assert self.message_normalization is None

                # Aggregate messages in target
                new_x.index_add_(dim=0, index=targets_r, source=m_r)

            # Self-loop
            self_w = self._get_relation_weights(i_layer=i, r=self.num_relations)
            if node_keep_mask is None:
                new_x += new_x @ self_w
            else:
                new_x[node_keep_mask] += new_x[node_keep_mask] @ self_w

            # Apply bias, if requested
            if self.use_bias:
                bias = self.biases[i]
                new_x += bias

            # Apply batch normalization, if requested
            if self.use_batch_norm:
                batch_norm = self.batch_norms[i]
                new_x = batch_norm(new_x)

            # Apply non-linearity
            if self.activations is not None:
                activation = self.activations[i]
                new_x = activation(new_x)

            x = new_x

        if batch is None and self.buffer_messages:
            self.enriched_embeddings = x

        return x

    def _get_relation_weights(self, i_layer: int, r: int) -> torch.FloatTensor:
        if self.decomposition == 'block':
            # allocate weight
            w = torch.zeros(self.embedding_dim, self.embedding_dim, device=self.device)

            # Get blocks
            this_layer_blocks = self.bases[i_layer]

            # self.bases[i_layer].shape (num_relations, num_blocks, embedding_dim/num_blocks, embedding_dim/num_blocks)
            # note: embedding_dim is guaranteed to be divisible by num_bases in the constructor
            block_size = self.embedding_dim // self.num_bases
            for b, start in enumerate(range(0, self.embedding_dim, block_size)):
                stop = start + block_size
                w[start:stop, start:stop] = this_layer_blocks[r, b, :, :]

        elif self.decomposition == 'basis':
            # The current basis weights, shape: (num_bases)
            att = self.att[i_layer][r, :]
            # the current bases, shape: (num_bases, embedding_dim, embedding_dim)
            b = self.bases[i_layer]
            # compute the current relation weights, shape: (embedding_dim, embedding_dim)
            w = torch.sum(att[:, None, None] * b, dim=0)

        else:
            raise AssertionError(f'Unknown decomposition: {self.decomposition}')

        return w

    def score_hrt(self, hrt_batch: torch.LongTensor) -> torch.FloatTensor:  # noqa: D102
        # Enrich only required embeddings
        x = self._enrich_embeddings(batch=hrt_batch if self.sparse_messages_owa else None)

        # Get embeddings
        h = x[hrt_batch[:, 0]]
        r = self._relation_embeddings(hrt_batch[:, 1])
        t = x[hrt_batch[:, 2]]

        return self._interaction_function(h=h, r=r, t=t).view(-1, 1)

    def score_t(self, hr_batch: torch.LongTensor) -> torch.FloatTensor:  # noqa: D102
        x = self._enrich_embeddings()

        # Get embeddings
        h = x[hr_batch[:, 0]].view(-1, 1, self.embedding_dim)
        r = self._relation_embeddings(hr_batch[:, 1]).view(-1, 1, self.embedding_dim)
        t = x.view(1, -1, self.embedding_dim)

        return self._interaction_function(h=h, r=r, t=t)

    def score_h(self, rt_batch: torch.LongTensor) -> torch.FloatTensor:  # noqa: D102
        x = self._enrich_embeddings()

        # Get embeddings
        h = x.view(1, -1, self.embedding_dim)
        r = self._relation_embeddings(rt_batch[:, 0]).view(-1, 1, self.embedding_dim)
        t = x[rt_batch[:, 1]].view(-1, 1, self.embedding_dim)

        return self._interaction_function(h=h, r=r, t=t)