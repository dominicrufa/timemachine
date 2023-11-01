# Water sampling script that tests that we can use an instantaneous monte carlo
# mover to insert/delete waters from a buckyball

# enable for 2x slow down
# from jax import config
# config.update("jax_enable_x64", True)

import argparse
import sys

import numpy as np
from rdkit import Chem
from water_sampling_common import (
    DEFAULT_BB_RADIUS,
    compute_density,
    compute_occupancy,
    get_initial_state,
    setup_forcefield,
)

from timemachine.constants import DEFAULT_TEMP
from timemachine.fe import cif_writer
from timemachine.fe.free_energy import image_frames
from timemachine.md.barostat.moves import NPTMove
from timemachine.md.exchange import exchange_mover
from timemachine.md.states import CoordsVelBox


def image_xvb(initial_state, xvb_t):
    new_coords = image_frames(initial_state, [xvb_t.coords], [xvb_t.box])[0]
    return CoordsVelBox(new_coords, xvb_t.velocities, xvb_t.box)


def test_exchange():
    parser = argparse.ArgumentParser(description="Test the exchange protocol in a box of water.")
    parser.add_argument("--water_pdb", type=str, help="Location of the water PDB", required=True)
    parser.add_argument(
        "--ligand_sdf",
        type=str,
        help="SDF file containing the ligand of interest. Disable to run bulk water.",
        required=False,
    )
    parser.add_argument("--out_cif", type=str, help="Output cif file", required=True)
    parser.add_argument(
        "--md_steps_per_batch",
        type=int,
        help="Number of MD steps per batch",
        required=True,
    )

    parser.add_argument(
        "--mc_steps_per_batch",
        type=int,
        help="Number of MC steps per batch",
        required=True,
    )

    parser.add_argument(
        "--insertion_type",
        type=str,
        help='Allowed values "targeted" and "untargeted"',
        required=True,
        choices=["targeted", "untargeted"],
    )
    parser.add_argument(
        "--use_hmr",
        type=int,
        help="Whether or not we apply HMR. 1 for yes, 0 for no.",
        required=True,
    )

    args = parser.parse_args()

    print(" ".join(sys.argv))

    if args.ligand_sdf is not None:
        suppl = list(Chem.SDMolSupplier(args.ligand_sdf, removeHs=False))
        mol = suppl[0]
    else:
        mol = None

    ff = setup_forcefield()
    seed = 2024
    np.random.seed(seed)

    nb_cutoff = 1.2  # this has to be 1.2 since the builders hard code this in (should fix later)
    # nit: use lamb=0.0 to get the fully-interacting end-state
    initial_state, nwm, topology = get_initial_state(args.water_pdb, mol, ff, seed, nb_cutoff, args.use_hmr, lamb=0.0)
    # set up water indices, assumes that waters are placed at the front of the coordinates.
    water_idxs = []
    for wai in range(nwm):
        water_idxs.append([wai * 3 + 0, wai * 3 + 1, wai * 3 + 2])
    water_idxs = np.array(water_idxs)
    bps = initial_state.potentials

    # [0] nb_all_pairs, [1] nb_ligand_water, [2] nb_ligand_protein
    # all_pairs has masked charges
    if mol:
        # uses a summed potential
        nb_beta = bps[-1].potential.potentials[1].beta
        nb_cutoff = bps[-1].potential.potentials[1].cutoff
        nb_water_ligand_params = bps[-1].potential.params_init[1]
        print("number of ligand atoms", mol.GetNumAtoms())
    else:
        # does not use a summed potential
        nb_beta = bps[-1].potential.beta
        nb_cutoff = bps[-1].potential.cutoff
        nb_water_ligand_params = bps[-1].params
    print("number of water atoms", nwm * 3)
    print("water_ligand parameters", nb_water_ligand_params)

    # tibd optimized
    if args.insertion_type == "targeted":
        exc_mover = exchange_mover.TIBDExchangeMove(
            nb_beta,
            nb_cutoff,
            nb_water_ligand_params,
            water_idxs,
            DEFAULT_TEMP,
            initial_state.ligand_idxs,
            DEFAULT_BB_RADIUS,
        )
    elif args.insertion_type == "untargeted":
        # vanilla reference
        exc_mover = exchange_mover.BDExchangeMove(nb_beta, nb_cutoff, nb_water_ligand_params, water_idxs, DEFAULT_TEMP)

    cur_box = initial_state.box0
    cur_x_t = initial_state.x0
    cur_v_t = np.zeros_like(cur_x_t)

    # debug
    seed = 2023
    if mol:
        writer = cif_writer.CIFWriter([topology, mol], args.out_cif)
    else:
        writer = cif_writer.CIFWriter([topology], args.out_cif)

    cur_x_t = image_frames(initial_state, [cur_x_t], [cur_box])[0]
    writer.write_frame(cur_x_t * 10)

    npt_mover = NPTMove(
        bps=initial_state.potentials,
        masses=initial_state.integrator.masses,
        temperature=initial_state.integrator.temperature,
        pressure=initial_state.barostat.pressure,
        n_steps=None,
        seed=seed,
        dt=initial_state.integrator.dt,
        friction=initial_state.integrator.friction,
        barostat_interval=initial_state.barostat.interval,
    )

    # equilibration
    print("Equilibrating the system... ", end="", flush=True)

    equilibration_steps = 50000
    # equilibrate using the npt mover
    npt_mover.n_steps = equilibration_steps
    xvb_t = CoordsVelBox(cur_x_t, cur_v_t, cur_box)
    xvb_t = npt_mover.move(xvb_t)
    print("done")

    # TBD: cache the minimized and equilibrated initial structure later on to iterate faster.
    npt_mover.n_steps = args.md_steps_per_batch
    # (ytz): If I start with pure MC, and no MD, it's actually very easy to remove the waters.
    # since the starting waters have very very high energy. If I re-run MD, then it becomes progressively harder
    # remove the water since we will re-equilibriate the waters.
    for idx in range(1000000):
        density = compute_density(nwm, xvb_t.box)

        xvb_t = image_xvb(initial_state, xvb_t)

        # start_time = time.time()
        for _ in range(args.mc_steps_per_batch):
            assert np.amax(np.abs(xvb_t.coords)) < 1e3
            xvb_t = exc_mover.move(xvb_t)

        # compute occupancy at the end of MC moves (as opposed to MD moves), as its more sensitive to any possible
        # biases and/or correctness issues.
        occ = compute_occupancy(xvb_t.coords, xvb_t.box, initial_state.ligand_idxs, threshold=DEFAULT_BB_RADIUS)
        print(
            f"{exc_mover.n_accepted} / {exc_mover.n_proposed} | density {density} | # of waters in spherical region {occ // 3} | md step: {idx * args.md_steps_per_batch}",
            flush=True,
        )

        if idx % 10 == 0:
            writer.write_frame(xvb_t.coords * 10)

        # print("time per mc move", (time.time() - start_time) / mc_steps_per_batch)

        # run MD
        xvb_t = npt_mover.move(xvb_t)

    writer.close()


if __name__ == "__main__":
    # A trajectory is written out called water.cif
    # To visualize it, run: pymol water.cif (note that the simulation does not have to be complete to visualize progress)

    # example invocation:

    # start with 0 waters, with hmr, using espaloma charges, 10k mc steps, 10k md steps, targeted insertion:
    # python -u examples/water_sampling_mc.py --water_pdb timemachine/datasets/water_exchange/bb_0_waters.pdb --ligand_sdf timemachine/datasets/water_exchange/bb_centered_espaloma.sdf --out_cif traj_0_waters.cif --md_steps_per_batch 10000 --mc_steps_per_batch 10000 --insertion_type targeted --use_hmr 1

    # start with 6 waters, with hmr, using espaloma charges, 10k mc steps, 10k md steps, targeted insertion:
    # python -u examples/water_sampling_mc.py --water_pdb timemachine/datasets/water_exchange/bb_6_waters.pdb --ligand_sdf timemachine/datasets/water_exchange/bb_centered_espaloma.sdf --out_cif traj_6_waters.cif --md_steps_per_batch 10000 --mc_steps_per_batch 10000 --insertion_type targeted --use_hmr 1

    # start with 0 waters, with hmr, using zero charges, 10k mc steps, 10k md steps, targeted insertion:
    # python -u examples/water_sampling_mc.py --water_pdb timemachine/datasets/water_exchange/bb_0_waters.pdb --ligand_sdf timemachine/datasets/water_exchange/bb_centered_neutral.sdf --out_cif traj_0_waters.cif --md_steps_per_batch 10000 --mc_steps_per_batch 10000 --insertion_type targeted --use_hmr 1

    # running in bulk, 10k mc steps, 10k md steps, untargeted insertion
    # python -u examples/water_sampling_mc.py --water_pdb timemachine/datasets/water_exchange/bb_0_waters.pdb --out_cif bulk.cif --md_steps_per_batch 10000 --mc_steps_per_batch 10000 --insertion_type untargeted
    test_exchange()