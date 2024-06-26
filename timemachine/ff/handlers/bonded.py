import numpy as np
import openmm as omm

from timemachine.ff.handlers.serialize import SerializableMixIn
from timemachine.ff.handlers.suffix import _SUFFIX
from timemachine.ff.handlers.utils import canonicalize_bond, match_smirks
from timemachine.ff.handlers.openmm_deserializer import (
    idxs_params_from_hb,
    idxs_params_from_ha,
    idxs_params_from_t,
    )

def generate_vd_idxs(mol, smirks):
    """
    Generate bonded indices using a valence dict. The indices generated
    are assumed to be reversible. i.e. reversing the indices evaluates to
    an identical energy. This is not intended to be used for ImproperTorsions.
    """

    vd = {}

    for p_idx, patt in enumerate(smirks):
        matches = match_smirks(mol, patt)
        for m in matches:
            sorted_m = canonicalize_bond(m)
            vd[sorted_m] = p_idx

    bond_idxs = np.array(list(vd.keys()), dtype=np.int32)
    param_idxs = np.array(list(vd.values()), dtype=np.int32)

    return bond_idxs, param_idxs

def query_pt_it_idxs_from_g(g):
    """query a heterograph (that has already parameterized an `openmm.System` object) for 
    indices of proper and improper torsions"""
    suffix = ""
    pt_counts = 0
    it_counts = 0
    
    # count pts; this is taken from espaloma's janky modifying of `openmm.System` objects
    for idx in range(g.heterograph.number_of_nodes("n4")):
        idx0 = g.nodes["n4"].data["idxs"][idx, 0].item()
        idx1 = g.nodes["n4"].data["idxs"][idx, 1].item()
        idx2 = g.nodes["n4"].data["idxs"][idx, 2].item()
        idx3 = g.nodes["n4"].data["idxs"][idx, 3].item()

        # assuming both (a,b,c,d) and (d,c,b,a) are listed for every torsion, only pick one of the orderings
        if idx0 < idx3:
            ks = g.nodes["n4"].data["k%s" % suffix][idx]
            for sub_idx in range(ks.flatten().shape[0]):
                k = ks[sub_idx].item()
                if k != 0.0:
                    pt_counts += 1

    # it counts; 
    if "k%s" % suffix in g.nodes["n4_improper"].data:
        for idx in range(
            g.heterograph.number_of_nodes("n4_improper")
        ):
            idx0 = g.nodes["n4_improper"].data["idxs"][idx, 0].item()
            idx1 = g.nodes["n4_improper"].data["idxs"][idx, 1].item()
            idx2 = g.nodes["n4_improper"].data["idxs"][idx, 2].item()
            idx3 = g.nodes["n4_improper"].data["idxs"][idx, 3].item()

            ks = g.nodes["n4_improper"].data["k%s" % suffix][idx]
            for sub_idx in range(ks.flatten().shape[0]):
                k = ks[sub_idx].item()
                if k != 0.0:
                    it_counts += 1
    return (
        np.arange(pt_counts, dtype=np.int32), 
        np.arange(pt_counts, it_counts + pt_counts, dtype=np.int32) # since pts are added first
    )

def prune_torsions(torsion_idxs, match_torsion_idxs):
    """return indices of arg0 for which there are any matches in arg1"""
    out_indices = []
    for _idx, torsion_idx in enumerate(torsion_idxs):
        matches = np.array(
            [np.any([np.all(torsion_idx == pt_idx), 
                     np.all(torsion_idx[::-1] == pt_idx)]) for pt_idx in match_torsion_idxs])
        any_matches = np.any(matches)
        if any_matches: out_indices.append(_idx)
    return np.array(out_indices, dtype=np.int32)  

def annotate_mol_sys_torsions(mol, omm_system, mol_graph, ff):
    """setattrs `pt_idxs`, `it_idxs` for a `Chem.ROMol` given it as a 
    `openmm_system` attr of type `openmm.System`;
    if `mol_graph` is None, proper/improper torsion indices will be found by 
    querying proper torsion smirks in the `ff` object;
    otherwise, the proper/improper torsion will be queried from the mol graph.
    """
    ptfs = [f for f in omm_system.getForces() if isinstance(f, omm.PeriodicTorsionForce)]
    assert len(ptfs) == 1, f"only 1 periodic torsion force is handled at this time"
    num_torsions = ptfs[0].getNumTorsions()
    omm_torsion_idxs, omm_assigned_params = idxs_params_from_t(ptfs)
    if mol_graph is None:
        pt_torsion_idxs, param_idxs = generate_vd_idxs(mol, ff.pt_handle.smirks)
        assert len(pt_torsion_idxs) == len(param_idxs)
        _proper_idxs = prune_torsions(omm_torsion_idxs, pt_torsion_idxs)
        _improper_idxs = np.array(
            [idx for idx in range(len(omm_torsion_idxs)) if idx not in _proper_idxs],
            dtype=np.int32)
    else:
        _proper_idxs, _improper_idxs = query_pt_it_idxs_from_g(mol_graph)
        assert len(_proper_idxs) + len(_improper_idxs) == num_torsions
        
    setattr(mol, 'openmm_system', omm_system)
    setattr(mol, 'pt_idxs', _proper_idxs)
    setattr(mol, 'it_idxs', _improper_idxs)

def handle_omm_torsions(mol, proper = True):
    ptfs = [f for f in mol.openmm_system.getForces() if isinstance(f, omm.PeriodicTorsionForce)]
    omm_torsion_idxs, omm_assigned_params = idxs_params_from_t(ptfs)
    _proper_idxs = mol.pt_idxs
    _improper_idxs = mol.it_idxs
    choice_idxs = mol.pt_idxs if proper else mol.it_idxs
    idxs = omm_torsion_idxs[choice_idxs,:]
    assigned_params = omm_assigned_params[choice_idxs,:]
    return assigned_params, idxs

# its trivial to re-use this for everything except the ImproperTorsions
class ReversibleBondHandler(SerializableMixIn):
    def __init__(self, smirks, params, props):
        """ "Reversible" here means that bond energy is symmetric to index reversal
        u_bond(x[i], x[j]) = u_bond(x[j], x[i])"""
        self.smirks = smirks
        self.params = np.array(params, dtype=np.float64)
        self.props = props
        assert len(self.smirks) == len(self.params)

    def lookup_smirks(self, query):
        for s_idx, s in enumerate(self.smirks):
            if s == query:
                return self.params[s_idx]

    def partial_parameterize(self, params, mol):
        return self.static_parameterize(params, self.smirks, mol)

    def parameterize(self, mol):
        return self.static_parameterize(self.params, self.smirks, mol)

    @staticmethod
    def static_parameterize(params, smirks, mol):
        """
        Parameterize given molecule

        Parameters
        ----------
        mol: Chem.ROMol
            rdkit molecule, should have hydrogens pre-added

        Returns
        -------
        tuple of (Q,2) (np.int32), ((Q,2), fn: R^Qx2 -> R^Px2))
            System bond idxes, parameters, and the vjp_fn.

        """

        bond_idxs, param_idxs = generate_vd_idxs(mol, smirks)
        return params[param_idxs], bond_idxs


# we need to subclass to get the names backout
class HarmonicBondHandler(ReversibleBondHandler):
    @staticmethod
    def static_parameterize(params, smirks, mol):
        if hasattr(mol, 'openmm_system'):
            hbfs = [f for f in mol.openmm_system.getForces() if isinstance(f, omm.HarmonicBondForce)]
            bond_idxs, mol_params = idxs_params_from_hb(hbfs)
        else:
            mol_params, bond_idxs = super(HarmonicBondHandler, HarmonicBondHandler).static_parameterize(params, smirks, mol)

        # validate expected set of bonds
        rd_bonds = set()
        for b in mol.GetBonds():
            rd_bonds.add(tuple(sorted([b.GetBeginAtomIdx(), b.GetEndAtomIdx()])))

        ff_bonds = set()
        for i, j in bond_idxs:
            ff_bonds.add(tuple(sorted([i, j])))

        if rd_bonds != ff_bonds:
            message = f"""Did not preserve the bond table of input mol!
            missing bonds (present in mol): {rd_bonds - ff_bonds}
            new bonds (not present in mol): {ff_bonds - rd_bonds}"""
            raise ValueError(message)

        # handle special case of 0 bonds
        if len(mol_params) == 0:
            mol_params = params[:0]  # empty slice with same dtype, other dimensions
            bond_idxs = np.zeros((0, 2), dtype=np.int32)

        return mol_params, bond_idxs


class HarmonicAngleHandler(ReversibleBondHandler):
    @staticmethod
    def static_parameterize(params, smirks, mol):
        if hasattr(mol, 'openmm_system'):
            hafs = [f for f in mol.openmm_system.getForces() if isinstance(f, omm.HarmonicAngleForce)]
            angle_idxs, mol_params = idxs_params_from_ha(hafs)
        else:
            mol_params, angle_idxs = super(HarmonicAngleHandler, HarmonicAngleHandler).static_parameterize(
                params, smirks, mol
            )
        if len(mol_params) == 0:
            mol_params = params[:0]  # empty slice with same dtype, other dimensions
            angle_idxs = np.zeros((0, 3), dtype=np.int32)
        return mol_params, angle_idxs
        

class ProperTorsionHandler:
    def __init__(self, smirks, params, props):
        """
        Parameters
        ----------
        smirks: list str
            list of smirks patterns

        params: list of list
            each torsion may have a variadic number of terms.

        """
        # self.smirks = smirks

        # raw_params = params # internals is a
        self.counts = []
        self.smirks = []
        self.params = []
        for smi, terms in zip(smirks, params):
            self.smirks.append(smi)
            self.counts.append(len(terms))
            for term in terms:
                self.params.append(term)

        self.counts = np.array(self.counts, dtype=np.int32)

        self.params = np.array(self.params, dtype=np.float64)
        self.props = props

    def parameterize(self, mol):
        return self.static_parameterize(self.params, self.smirks, self.counts, mol)

    def partial_parameterize(self, params, mol):
        return self.static_parameterize(params, self.smirks, self.counts, mol)

    @staticmethod
    def static_parameterize(params, smirks, counts, mol):
        if hasattr(mol, 'openmm_system'):
            assigned_params, proper_idxs = handle_omm_torsions(mol, proper = True)
        else:
            torsion_idxs, param_idxs = generate_vd_idxs(mol, smirks)
            assert len(torsion_idxs) == len(param_idxs)
            scatter_idxs = []
            repeats = []

            # prefix sum of size + 1
            pfxsum = np.concatenate([[0], np.cumsum(counts)])
            for p_idx in param_idxs:
                start = pfxsum[p_idx]
                end = pfxsum[p_idx + 1]
                scatter_idxs.extend((range(start, end)))
                repeats.append(counts[p_idx])

            # for k, _, _ in params[scatter_idxs]:
            # if k == 0.0:
            # print("WARNING: zero force constant torsion generated.")

            scatter_idxs = np.array(scatter_idxs)

            # if no matches found, return arrays that can still be concatenated as expected
            if len(param_idxs) > 0:
                assigned_params = params[scatter_idxs]
                proper_idxs = np.repeat(torsion_idxs, repeats, axis=0).astype(np.int32)
            else:
                assigned_params = params[:0]  # empty slice with same dtype, other dimensions
                proper_idxs = np.zeros((0, 4), dtype=np.int32)

        return assigned_params, proper_idxs

    def serialize(self):
        list_params = []
        counter = 0
        for smi_idx, smi in enumerate(self.smirks):
            t_params = []
            for _ in range(self.counts[smi_idx]):
                t_params.append(self.params[counter].tolist())
                counter += 1
            list_params.append(t_params)

        key = type(self).__name__[: -len(_SUFFIX)]
        patterns = []
        for smi, p in zip(self.smirks, list_params):
            patterns.append((smi, p))

        body = {"patterns": patterns}
        result = {key: body}

        return result


class ImproperTorsionHandler(SerializableMixIn):
    def __init__(self, smirks, params, props):
        self.smirks = smirks
        self.params = np.array(params, dtype=np.float64)
        self.props = props
        assert self.params.shape[1] == 3
        assert len(self.smirks) == len(self.params)

    def partial_parameterize(self, params, mol):
        return self.static_parameterize(params, self.smirks, mol)

    def parameterize(self, mol):
        return self.static_parameterize(self.params, self.smirks, mol)

    @staticmethod
    def static_parameterize(params, smirks, mol):
        # improper torsions do not use a valence dict as
        # we cannot sort based on b_idxs[0] and b_idxs[-1]
        # and reverse if needed. Impropers are centered around
        # the first atom.
        impropers = dict()

        def make_key(idxs):
            assert len(idxs) == 4
            # pivot around the center
            ctr = idxs[1]
            # sort the neighbors so they're unique
            nbs = idxs[0], idxs[2], idxs[3]
            nbs = sorted(nbs)
            return nbs[0], ctr, nbs[1], nbs[2]

        for p_idx, patt in enumerate(smirks):
            matches = match_smirks(mol, patt)

            for m in matches:
                key = make_key(m)
                impropers[key] = p_idx

        improper_idxs = []
        param_idxs = []

        for atom_idxs, p_idx in impropers.items():
            center = atom_idxs[1]
            others = [atom_idxs[0], atom_idxs[2], atom_idxs[3]]
            for p in [(others[i], others[j], others[k]) for (i, j, k) in [(0, 1, 2), (1, 2, 0), (2, 0, 1)]]:
                improper_idxs.append(canonicalize_bond((center, p[0], p[1], p[2])))
                param_idxs.append(p_idx)

        param_idxs = np.array(param_idxs)

        # if no matches found, return arrays that can still be concatenated as expected
        if len(param_idxs) > 0:
            assigned_params = params[param_idxs]
            improper_idxs = np.array(improper_idxs, dtype=np.int32)
        else:
            assigned_params = params[:0]  # empty slice with same dtype, other dimensions
            improper_idxs = np.zeros((0, 4), dtype=np.int32)
    
        return assigned_params, improper_idxs
