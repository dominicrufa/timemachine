import jax.numpy as jnp
import numpy as np
from jax import vmap
from jax.scipy.special import logsumexp
from numpy.typing import NDArray
from typing_extensions import TypeAlias

Array: TypeAlias = NDArray


def get_all_pairs_indices(n: int) -> Array:
    """all indices i, j such that i < j < n"""
    n_interactions = n * (n - 1) / 2

    pairs = np.stack(np.triu_indices(n, k=1)).T

    assert pairs.shape == (n_interactions, 2)

    return pairs


def pairs_from_interaction_groups(group_a_indices: Array, group_b_indices: Array) -> Array:
    """(a, b) for a in group_a_indices, b in group_b_indices"""
    n_interactions = len(group_a_indices) * len(group_b_indices)

    pairs = np.stack(np.meshgrid(group_a_indices, group_b_indices)).reshape(2, -1).T

    assert pairs.shape == (n_interactions, 2)

    return pairs


def compute_lifting_parameter(lamb, lambda_plane_idxs, lambda_offset_idxs, cutoff):
    """One way to compute a per-particle "4D" offset in terms of an adjustable lamb and
    constant per-particle parameters.

    Notes
    -----
    (ytz): this initializes the 4th dimension to a fixed plane adjust by an offset
    followed by a scaling by cutoff.

    lambda_plane_idxs are typically 0 or 1 and allows us to turn off an interaction
    independent of the lambda value.

    lambda_offset_idxs are typically 0 and 1, and allows us to adjust the w coordinate
    in a lambda-dependent way.
    """

    w = cutoff * (lambda_plane_idxs + lambda_offset_idxs * lamb)
    return w


def delta_r(ri, rj, box=None):
    diff = ri - rj  # this can be either N,N,3 or B,3

    # box is None for harmonic bonds, not None for nonbonded terms
    if box is not None:
        box_diag = jnp.diag(box)
        diff -= box_diag * jnp.floor(diff / box_diag + 0.5)
    return diff


def distance_on_pairs(
    ri,
    rj,
    box=None,
    w_offsets=None,  # per-pair 4-d offset
):
    """O(n) where n = len(ri) = len(rj)

    Notes
    -----
    TODO [performance]: any difference if the signature is (conf, pairs) rather than (ri, rj)?
    """
    assert len(ri) == len(rj)

    diff = delta_r(ri, rj, box)
    if w_offsets is not None:
        diff = jnp.concatenate([diff, jnp.array(w_offsets).reshape(-1, 1)], axis=1)

    dij = jnp.linalg.norm(diff, axis=-1)

    assert len(dij) == len(ri)

    return dij


def get_interacting_pair_indices_batch(confs, boxes, pairs, cutoff=1.2):
    """Given candidate interacting pairs, exclude most pairs whose distances are >= cutoff

    Parameters
    ----------
    confs: (n_snapshots, n_atoms, dim) float array
    boxes: (n_snapshots, dim, dim) float array
    pairs: (n_candidate_pairs, 2) integer array
    cutoff: float

    Returns
    -------
    batch_pairs : (len(confs), max_n_neighbors, 2) array
        where max_n_neighbors pairs are returned for each conf in confs

    Notes
    -----
    * Padding causes some amount of wasted effort, but keeps things nice and fixed-dimensional for later XLA steps
    """
    n_snapshots, n_atoms, dim = confs.shape
    assert boxes.shape == (n_snapshots, dim, dim)

    distances = vmap(distance_on_pairs)(confs[:, pairs[:, 0]], confs[:, pairs[:, 1]], boxes)
    assert distances.shape == (len(confs), len(pairs))

    neighbor_masks = distances < cutoff
    # how many total neighbors?

    n_neighbors = jnp.sum(neighbor_masks, 1)
    max_n_neighbors = max(n_neighbors)

    assert max_n_neighbors > 0

    # sorting in order of [falses, ..., trues]
    keep_inds = np.argsort(neighbor_masks, axis=1)[:, -max_n_neighbors:]
    batch_pairs = pairs[keep_inds]

    assert batch_pairs.shape == (len(confs), max_n_neighbors, 2)

    return batch_pairs


def pairwise_distances(x, box, w=None):
    """
    Compute the (N, N) periodic distance matrix given an (N, D) array of
    coordinates.

    Optionally accepts an (N, 1) array of coordinates in the "lifting"
    (typically 4th) dimension; if passed, computes distances assuming periodic
    boundaries for the primary dimensions and aperiodicty in the lifting
    dimension.

    Parameters
    ----------
    x : ndarray (N, D)
        input coordinates

    box : ndarray (D, D)
        dimensions of periodic box

    w : ndarray (N, 1), optional
        coordinates in aperiodic lifting dimension
    """
    n, d = x.shape
    assert box.shape == (d, d)
    if w is not None:
        assert w.shape[0] == n

    d_ijk = delta_r(x[:, None], x[None, :], box)  # (x_i, x_j, dimension)
    d2_ij = jnp.sum(d_ijk ** 2, axis=2)

    if w is not None:
        dw_ij = w[:, None] - w[None, :]
        d2_ij += dw_ij ** 2

    # prevent nans in gradient
    d2_ij = d2_ij.at[jnp.diag_indices_from(d2_ij)].set(0.0)

    d_ij = jnp.sqrt(d2_ij)
    return d_ij


def distance_from_one_to_others(x_i, x_others, box=None, cutoff=jnp.inf):
    """[d(x_i, x_j, box) for x_j in x_others]

    Parameters
    ----------
    x_i : [dim] array
    x_others: [N, dim] array
        where dim = 3 or 4
    box : optional diagonal [dim, dim] array
    cutoff: float

    Returns
    -------
    d_ij : [N] array
        array of distances from x_i to each [x_j in x_others]
        if distance(x_i, x_j) > cutoff, d_ij is set to np.inf
    """
    displacements_ij = delta_r(x_i, x_others, box)
    d2_ij = jnp.sum(displacements_ij ** 2, axis=1)
    d_ij = jnp.where(d2_ij <= cutoff ** 2, jnp.sqrt(d2_ij), jnp.inf)
    return d_ij


def bernoulli_logpdf(log_p_i, z_i) -> float:
    """log( prod_i (z_i * p_i) + (1 - z_i) * (1 - p_i) )
    where z_i are boolean outcomes, p_i are probabilities [0,1]"""

    n = len(z_i)
    assert z_i.shape == (n,)
    assert log_p_i.shape == (n,)
    # implement subtraction log(1 - p_i) using logsumexp
    # * a[0] = log(1.0), a[1] = log(p_i)
    a = jnp.array([jnp.zeros(n), log_p_i])
    # * b[0] = 1, b[1] = -1
    b = jnp.array([jnp.ones(n), -jnp.ones(n)])
    log_1_minus_p_i = logsumexp(a=a, b=b, axis=0)

    return jnp.sum(jnp.where(z_i, log_p_i, log_1_minus_p_i))
