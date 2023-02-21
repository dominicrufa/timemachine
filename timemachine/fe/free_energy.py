from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple, Union, overload

import numpy as np
from numpy.typing import NDArray

from timemachine.constants import BOLTZ
from timemachine.fe import model_utils, topology
from timemachine.fe.bar import bar_with_bootstrapped_uncertainty, df_err_from_ukln, pair_overlap_from_ukln
from timemachine.fe.energy_decomposition import EnergyDecomposedState, compute_energy_decomposed_u_kln, get_batch_u_fns
from timemachine.fe.plots import make_dG_errs_figure, make_overlap_detail_figure, make_overlap_summary_figure
from timemachine.fe.stored_arrays import StoredArrays
from timemachine.fe.utils import get_mol_masses, get_romol_conf
from timemachine.ff import ForcefieldParams
from timemachine.ff.handlers import openmm_deserializer
from timemachine.lib import LangevinIntegrator, MonteCarloBarostat, custom_ops
from timemachine.lib.potentials import CustomOpWrapper, HarmonicBond
from timemachine.md.barostat.utils import compute_box_center, get_bond_list, get_group_indices


class HostConfig:
    def __init__(self, omm_system, conf, box):
        self.omm_system = omm_system
        self.conf = conf
        self.box = box


@dataclass
class MDParams:
    n_frames: int
    n_eq_steps: int
    steps_per_frame: int


@dataclass
class InitialState:
    """
    An initial contains everything that is needed to bitwise reproduce a trajectory given MDParams

    This object can be pickled safely.
    """

    potentials: List[CustomOpWrapper]
    integrator: LangevinIntegrator
    barostat: Optional[MonteCarloBarostat]
    x0: np.ndarray
    v0: np.ndarray
    box0: np.ndarray
    lamb: float
    ligand_idxs: np.ndarray


@dataclass
class SimulationResult:
    all_dGs: List[float]  # L - 1
    all_errs: List[float]  # L - 1
    dG_errs_by_lambda_by_component: np.ndarray  # (len(U_names), L - 1)
    overlaps_by_lambda: List[float]  # L - 1
    overlaps_by_lambda_by_component: np.ndarray  # (len(U_names), L - 1)
    dG_errs_png: bytes
    overlap_summary_png: bytes
    overlap_detail_png: bytes
    frames: List[Union[np.ndarray, StoredArrays]]  # (len(keep_idxs), n_frames, N, 3)
    boxes: List[np.ndarray]
    initial_states: List[InitialState]
    md_params: MDParams


def image_frames(initial_state: InitialState, frames: Sequence[np.ndarray], boxes: np.ndarray) -> np.ndarray:
    """Images a sequence of frames within the periodic box given an Initial state. Recenters the simulation around the
    centroid of the coordinates specified by initial_state.ligand_idxs prior to imaging.

    Calling this function on a sequence of frames will NOT produce identical energies/du_dp/du_dx. Should only be used
    for visualization convenience.

    Parameters
    ----------

    initial_state: InitialState
        State that the frames came from

    frames: sequence of np.ndarray of coordinates
        Coordinates to image, sequence of K arrays with shape (N, 3)

    boxes: list of boxes
        Boxes to image coordinates into, list of K arrays with shape (3, 3)

    Returns
    -------
        imaged_coordinates
    """
    assert np.array(boxes).shape[1:] == (3, 3), "Boxes are not 3x3"
    assert len(frames) == len(boxes), "Number of frames and boxes don't match"

    hb_potential = next(p for p in initial_state.potentials if isinstance(p, HarmonicBond))
    group_indices = get_group_indices(get_bond_list(hb_potential))
    imaged_frames = np.empty_like(frames)
    for i, (frame, box) in enumerate(zip(frames, boxes)):
        assert frame.ndim == 2 and frame.shape[-1] == 3, "frames must have shape (N, 3)"
        # Recenter the frame around the centroid of the ligand
        ligand_centroid = np.mean(frame[initial_state.ligand_idxs], axis=0)
        center = compute_box_center(box)
        offset = ligand_centroid + center
        centered_frames = frame - offset

        imaged_frames[i] = model_utils.image_frame(group_indices, centered_frames, box)
    return np.array(imaged_frames)


class BaseFreeEnergy:
    @staticmethod
    def _get_system_params_and_potentials(ff_params: ForcefieldParams, topology, lamb: float):
        params_potential_pairs = [
            topology.parameterize_harmonic_bond(ff_params.hb_params),
            topology.parameterize_harmonic_angle(ff_params.ha_params),
            topology.parameterize_periodic_torsion(ff_params.pt_params, ff_params.it_params),
            topology.parameterize_nonbonded(ff_params.q_params, ff_params.q_params_intra, ff_params.lj_params, lamb),
        ]

        params, potentials = zip(*params_potential_pairs)
        return params, potentials


# this class is serializable.
class AbsoluteFreeEnergy(BaseFreeEnergy):
    def __init__(self, mol, top):
        """
        Compute the absolute free energy of a molecule via 4D decoupling.

        Parameters
        ----------
        mol: rdkit mol
            Ligand to be decoupled

        top: Topology
            topology.Topology to use

        """
        self.mol = mol
        self.top = top

    def prepare_host_edge(self, ff_params: ForcefieldParams, host_system, lamb: float):
        """
        Prepares the host-guest system

        Parameters
        ----------
        ff_params: ForcefieldParams
            forcefield parameters

        host_system: openmm.System
            openmm System object to be deserialized.

        lamb: float
            alchemical parameter controlling 4D decoupling

        Returns
        -------
        3-tuple
            unbound_potentials, system_params, combined_masses

        """
        ligand_masses = get_mol_masses(self.mol)

        host_bps, host_masses = openmm_deserializer.deserialize_system(host_system, cutoff=1.2)
        hgt = topology.HostGuestTopology(host_bps, self.top)

        final_params, final_potentials = self._get_system_params_and_potentials(ff_params, hgt, lamb)
        combined_masses = self._combine(ligand_masses, host_masses)
        return final_potentials, final_params, combined_masses

    def prepare_vacuum_edge(self, ff_params: ForcefieldParams):
        """
        Prepares the vacuum system

        Parameters
        ----------
        ff_params: ForcefieldParams
            forcefield parameters

        Returns
        -------
        3-tuple
            unbound_potentials, system_params, combined_masses

        """
        ligand_masses = get_mol_masses(self.mol)
        final_params, final_potentials = self._get_system_params_and_potentials(ff_params, self.top, 0.0)
        return final_potentials, final_params, ligand_masses

    def prepare_combined_coords(self, host_coords=None):
        """
        Returns the combined coordinates.

        Parameters
        ----------
        host_coords: np.array
            Nx3 array of atomic coordinates
            If None, return just the ligand coordinates.

        Returns
        -------
            combined_coordinates
        """
        ligand_coords = get_romol_conf(self.mol)
        return self._combine(ligand_coords, host_coords)

    def _combine(self, ligand_values, host_values=None):
        """
        Combine the values along the 0th axis.
        The host values will be first, if given.
        Then ligand values.

        Parameters
        ----------
        ligand_values: np.array
        host_values: Optional[np.array]

        Returns
        -------
            combined_values
        """
        if host_values is None:
            return ligand_values
        return np.concatenate([host_values, ligand_values])


def batches(n: int, batch_size: int) -> Iterable[int]:
    assert n >= 0
    assert batch_size > 0
    quot, rem = divmod(n, batch_size)
    for _ in range(quot):
        yield batch_size
    if rem:
        yield rem


@overload
def sample(initial_state: InitialState, md_params: MDParams) -> Tuple[NDArray, NDArray]:
    ...


@overload
def sample(initial_state: InitialState, md_params: MDParams, max_buffer_frames: int) -> Tuple[StoredArrays, NDArray]:
    ...


def sample(initial_state: InitialState, md_params: MDParams, max_buffer_frames: Optional[int] = None):
    """Generate a trajectory given an initial state and a simulation protocol

    Parameters
    ----------
    initial_state: InitialState
        (contains potentials, integrator, optional barostat)
    md_params: MDParams
        (specifies x0, v0, box0, number of MD steps, thinning interval, etc...)

    Returns
    -------
    xs, boxes: np.arrays with .shape[0] = md_params.n_frames

    Notes
    -----
    * Assertion error if coords become NaN
    """

    bound_impls = [p.bound_impl(np.float32) for p in initial_state.potentials]
    intg_impl = initial_state.integrator.impl()
    if initial_state.barostat:
        baro_impl = initial_state.barostat.impl(bound_impls)
    else:
        baro_impl = None

    ctxt = custom_ops.Context(
        initial_state.x0,
        initial_state.v0,
        initial_state.box0,
        intg_impl,
        bound_impls,
        baro_impl,
    )

    # burn-in
    ctxt.multiple_steps_U(
        n_steps=md_params.n_eq_steps,
        store_u_interval=0,
        store_x_interval=0,
    )

    assert np.all(np.isfinite(ctxt.get_x_t())), "Equilibration resulted in a nan"

    def run_production_steps(n_steps: int) -> Tuple[NDArray, NDArray]:
        _, coords, boxes = ctxt.multiple_steps_U(
            n_steps=n_steps,
            store_u_interval=0,
            store_x_interval=md_params.steps_per_frame,
        )
        return coords, boxes

    all_coords: Union[NDArray, StoredArrays]

    if max_buffer_frames:
        all_coords = StoredArrays()
        all_boxes_: List[NDArray] = []
        for n_frames in batches(md_params.n_frames, max_buffer_frames):
            batch_coords, batch_boxes = run_production_steps(n_frames * md_params.steps_per_frame)
            all_coords.extend(batch_coords)
            all_boxes_.extend(batch_boxes)
        all_boxes = np.array(all_boxes_)
    else:
        all_coords, all_boxes = run_production_steps(md_params.n_frames * md_params.steps_per_frame)

    assert len(all_coords) == md_params.n_frames
    assert len(all_boxes) == md_params.n_frames

    assert np.all(np.isfinite(all_coords[-1])), "Production resulted in a nan"

    return all_coords, all_boxes


def estimate_free_energy_given_initial_states(
    initial_states,
    md_params,
    temperature,
    prefix,
    keep_idxs,
):
    """
    Estimate free energies given pre-generated samples. This implements the pair-BAR method, where
    windows assumed to be ordered with good overlap, with the final free energy being a sum
    of the components. The constants below are:

    L: the number of lambda windows
    T: the number of samples
    N: the number of atoms
    P: the number of components in the energy function.

    Parameters
    ----------
    initial_states: list of InitialState
        Initial state objects

    md_params: MDParams
        Detailing specifics of each simulation

    temperature: float
        Temperature the system was run at

    prefix: str
        A prefix that we append to the BAR overlap figures

    keep_idxs: list of int
        Which states we keep samples for. Must be positive.

    Return
    ------
    SimulationResult
        object containing results of the simulation

    """

    # run n_lambdas simulations in sequence
    u_kln_by_component_by_lambda, stored_frames, stored_boxes = run_sequential_sims_given_initial_states(
        initial_states, md_params, temperature, keep_idxs
    )

    # "pair BAR" free energy analysis
    kBT = BOLTZ * temperature
    beta = 1 / kBT

    all_dGs = []
    all_errs = []
    for lamb_idx, u_kln_by_component in enumerate(u_kln_by_component_by_lambda):
        # pair BAR
        u_kln = u_kln_by_component.sum(0)

        w_fwd = u_kln[1, 0] - u_kln[0, 0]
        w_rev = u_kln[0, 1] - u_kln[1, 1]

        df, df_err = bar_with_bootstrapped_uncertainty(w_fwd, w_rev)  # reduced units
        dG, dG_err = df / beta, df_err / beta  # kJ/mol

        message = f"{prefix} BAR: lambda {lamb_idx} -> {lamb_idx + 1} dG: {dG:.3f} +- {dG_err:.3f} kJ/mol"
        print(message, flush=True)

        all_dGs.append(dG)
        all_errs.append(dG_err)

    # (energy components, lambdas, energy fxns = 2, sampled states = 2, frames)
    ukln_by_lambda_by_component = np.array(u_kln_by_component_by_lambda).swapaxes(0, 1)

    # compute more diagnostics
    overlaps_by_lambda = [pair_overlap_from_ukln(u_kln) for u_kln in ukln_by_lambda_by_component.sum(axis=0)]

    dG_errs_by_lambda_by_component = np.array(
        [[df_err_from_ukln(u_kln) / beta for u_kln in ukln_by_lambda] for ukln_by_lambda in ukln_by_lambda_by_component]
    )
    overlaps_by_lambda_by_component = np.array(
        [[pair_overlap_from_ukln(u_kln) for u_kln in ukln_by_lambda] for ukln_by_lambda in ukln_by_lambda_by_component]
    )

    # generate figures
    U_names = [type(U_fn).__name__ for U_fn in initial_states[0].potentials]
    lambdas = [s.lamb for s in initial_states]

    overlap_detail_png = make_overlap_detail_figure(
        U_names,
        all_dGs,
        all_errs,
        u_kln_by_component_by_lambda,
        temperature,
        prefix,
    )
    dG_errs_png = make_dG_errs_figure(U_names, lambdas, all_errs, dG_errs_by_lambda_by_component)
    overlap_summary_png = make_overlap_summary_figure(
        U_names, lambdas, overlaps_by_lambda, overlaps_by_lambda_by_component
    )

    return SimulationResult(
        all_dGs,
        all_errs,
        dG_errs_by_lambda_by_component,
        overlaps_by_lambda,
        overlaps_by_lambda_by_component,
        dG_errs_png,
        overlap_summary_png,
        overlap_detail_png,
        stored_frames,
        stored_boxes,
        initial_states,
        md_params,
    )


def run_sequential_sims_given_initial_states(
    initial_states,
    md_params,
    temperature,
    keep_idxs,
):
    """Sequentially run simulations at each state in initial_states,
    returning summaries that can be used for pair BAR, energy decomposition, and other diagnostics

    Returns
    -------
    decomposed_u_klns: [n_lams - 1, n_components, 2, 2, n_frames] array
    stored_frames: coord trajectories, one for each state in keep_idxs
    stored_boxes: box trajectories, one for each state in keep_idxs

    Notes
    -----
    * Memory complexity:
        Memory demand should be no more than that of 2 states worth of frames.

        Requesting too many states in keep_idxs may blow this up,
        so best to keep to first and last states in keep_idxs.

        This restriction may need to be relaxed in the future if:
        * We decide to use MBAR(states) rather than sum_i BAR(states[i], states[i+1])
        * We use online protocol optimization approaches that require more states to be kept on-hand
    """
    stored_frames = []
    stored_boxes = []

    tmp_states = [None] * len(initial_states)

    # u_kln matrix (2, 2, n_frames) for each pair of adjacent lambda windows and energy term
    u_kln_by_component_by_lambda = []

    keep_idxs = keep_idxs or []
    if keep_idxs:
        assert all(np.array(keep_idxs) >= 0)

    for lamb_idx, initial_state in enumerate(initial_states):

        # run simulation
        cur_frames, cur_boxes = sample(initial_state, md_params)
        print(f"completed simulation at lambda={initial_state.lamb}!")

        # keep samples from any requested states in memory
        if lamb_idx in keep_idxs:
            stored_frames.append(cur_frames)
            stored_boxes.append(cur_boxes)

        # construct EnergyDecomposedState for current lamb_idx,
        # but keep no more than 2 of these states in memory at once
        if lamb_idx >= 2:
            tmp_states[lamb_idx - 2] = None

        bound_impls = [p.bound_impl(np.float32) for p in initial_state.potentials]
        cur_batch_U_fns = get_batch_u_fns(bound_impls, temperature)

        tmp_states[lamb_idx] = EnergyDecomposedState(cur_frames, cur_boxes, cur_batch_U_fns)

        # analysis that depends on current and previous state
        if lamb_idx > 0:
            state_pair = [tmp_states[lamb_idx - 1], tmp_states[lamb_idx]]
            u_kln_by_component = compute_energy_decomposed_u_kln(state_pair)
            u_kln_by_component_by_lambda.append(u_kln_by_component)

    return np.array(u_kln_by_component_by_lambda), stored_frames, stored_boxes
