"""Classes to perform Fourier and Linear interpolation."""

from collections import defaultdict
from typing import Dict, Optional

import numpy as np
from monty.json import MSONable
from pymatgen.electronic_structure.bandstructure import BandStructure
from pymatgen.electronic_structure.core import Spin

__all__ = ["Interpolator", "PeriodicLinearInterpolator"]


class Interpolator(MSONable):
    """Class to perform Fourier interpolation of electronic band structures."""

    def __init__(
        self,
        band_structure: BandStructure,
        magmom: Optional[np.ndarray] = None,
        mommat: Optional[np.ndarray] = None,
    ):
        """Interpolator to perform Fourier interpolation of electronic band structures.

        Interpolation is performed using BoltzTraP2.

        Args:
            band_structure: The Bandstructure object to be interpolated.
            magmom: Magnetic moments of the atoms.
            mommat: Momentum matrix, as supported by BoltzTraP2.
        """
        from BoltzTraP2.units import Angstrom
        from pymatgen.io.ase import AseAtomsAdaptor

        from ifermi.kpoints import get_kpoints_from_bandstructure

        self._band_structure = band_structure
        self._spins = self._band_structure.bands.keys()
        self._lattice_matrix = band_structure.structure.lattice.matrix.T * Angstrom
        self._projection_coefficients = defaultdict(dict)

        self._kpoints = get_kpoints_from_bandstructure(band_structure)
        self._atoms = AseAtomsAdaptor.get_atoms(band_structure.structure)

        self._magmom = magmom
        self._mommat = mommat
        self._structure = band_structure.structure

    def interpolate_bands(
        self,
        interpolation_factor: float = 5,
        return_velocities: bool = False,
        nworkers: int = -1,
    ):
        """Get an interpolated pymatgen band structure.

        Note, the interpolation mesh is determined using by ``interpolate_factor``
        option in the ``Interpolator`` constructor.

        The degree of parallelization is controlled by the ``nworkers`` option.

        Args:
            interpolation_factor: The factor by which the band structure will
                be interpolated.
            return_velocities: Whether to return the group velocities.
            nworkers: The number of processors used to perform the
                interpolation. If set to ``-1``, the number of workers will
                be set to the number of CPU cores.

        Returns:
            The interpolated electronic structure. If ``return_velocities`` is True,
            the group velocities will also be returned as a  dict of
            ``{Spin: velocoties}`` where velocities is a numpy array with the
            shape (nbands, nkpoints, 3) and has units of m/s.
        """
        import multiprocessing

        from BoltzTraP2 import fite, sphere
        from BoltzTraP2.units import eV
        from pymatgen.io.ase import AseAtomsAdaptor
        from spglib import spglib

        from ifermi.boltztrap import get_bands_fft
        from ifermi.kpoints import sort_boltztrap_to_spglib

        coefficients = {}

        equivalences = sphere.get_equivalences(
            atoms=self._atoms,
            nkpt=self._kpoints.shape[0] * interpolation_factor,
            magmom=self._magmom,
        )

        # get the interpolation mesh used by BoltzTraP2
        interpolation_mesh = 2 * np.max(np.abs(np.vstack(equivalences)), axis=0) + 1

        for spin in self._spins:
            energies = self._band_structure.bands[spin] * eV
            data = DFTData(
                self._kpoints, energies, self._lattice_matrix, mommat=self._mommat
            )
            coefficients[spin] = fite.fitde3D(data, equivalences)

        nworkers = multiprocessing.cpu_count() if nworkers == -1 else nworkers

        energies = {}
        velocities = {}
        for spin in self._spins:
            energies[spin], velocities[spin] = get_bands_fft(
                equivalences,
                coefficients[spin],
                self._lattice_matrix,
                nworkers=nworkers,
            )

            # boltztrap2 gives energies in Rydberg, convert to eV
            energies[spin] /= eV

            # velocities in Bohr radius * Rydberg / hbar, convert to m/s.

        efermi = self._band_structure.efermi

        atoms = AseAtomsAdaptor().get_atoms(self._band_structure.structure)
        mapping, grid = spglib.get_ir_reciprocal_mesh(
            interpolation_mesh, atoms, symprec=0.1
        )
        kpoints = grid / interpolation_mesh

        # sort energies so they have the same order as the k-points generated by spglib
        sort_idx = sort_boltztrap_to_spglib(kpoints)
        energies = {s: ener[:, sort_idx] for s, ener in energies.items()}
        velocities = {s: vel[:, sort_idx] for s, vel in velocities.items()}

        rlat = self._band_structure.structure.lattice.reciprocal_lattice
        interp_band_structure = BandStructure(
            kpoints, energies, rlat, efermi, structure=self._structure
        )

        if return_velocities:
            return interp_band_structure, velocities

        return interp_band_structure


class DFTData:
    """DFTData object used for BoltzTraP2 interpolation."""

    def __init__(
        self,
        kpoints: np.ndarray,
        energies: np.ndarray,
        lattice_matrix: np.ndarray,
        mommat: Optional[np.ndarray] = None,
    ):
        """Initialize DFTData object used for BoltzTraP2 interpolation.

        Note that the units used by BoltzTraP are different to those used by VASP.

        Args:
            kpoints: The k-points in fractional coordinates.
            energies: The band energies in Hartree, formatted as (nbands, nkpoints).
            lattice_matrix: The lattice matrix in Bohr^3.
            mommat: The band structure derivatives.
        """
        self.kpoints = kpoints
        self.ebands = energies
        self.lattice_matrix = lattice_matrix
        self.volume = np.abs(np.linalg.det(self.lattice_matrix))
        self.mommat = mommat

    def get_lattvec(self) -> np.ndarray:
        """Get the lattice matrix. This method is required by BoltzTraP2."""
        return self.lattice_matrix


class PeriodicLinearInterpolator(object):
    """Class to perform linear interpolation of periodic properties."""

    def __init__(self, kpoints: np.ndarray, data: Dict[Spin, np.ndarray]):
        """
        Create an interpolator that can handle periodic boundary conditions.

        Args:
            kpoints: The k-points in fractional coordinations as a numpy array.
                with the shape (nkpoints, 3). Note, the k-points must cover
                the full Brillouin zone, not just the irredicible part.
            data: The data to interpolate. Should be given for spin up
                and spin down bands. If the system is not spin polarized
                then only spin up should be set. The data for each spin
                channel should be a numpy array with the shape
                (nbands, nkpoints, ...). The values to interpolate can be scalar
                or multidimensional.
        """
        grid_kpoints, mesh_dim, sort_idx = self._grid_kpoints(kpoints)
        self._setup_interpolators(data, grid_kpoints, mesh_dim, sort_idx)

    def interpolate(self, spin: Spin, bands: np.ndarray, kpoints: np.ndarray):
        """
        Get the interpolated data for a spin channel and series of bands and k-points.

        Args:
            spin: The spin channel.
            bands: A list of bands at which to interpolate.
            kpoints: A list of k-points at which to interpolate. The number of
                k-points must equal the number of bands.

        Returns:
            A list of interpolated values.
        """
        v = np.concatenate([np.asarray(bands)[:, None], np.asarray(kpoints)], axis=1)
        interp_data = self.interpolators[spin](v)
        return interp_data

    def _setup_interpolators(self, data, grid_kpoints, mesh_dim, sort_idx):
        from scipy.interpolate import RegularGridInterpolator

        x = grid_kpoints[:, 0, 0, 0]
        y = grid_kpoints[0, :, 0, 1]
        z = grid_kpoints[0, 0, :, 2]

        self.nbands = {s: c.shape[0] for s, c in data.items()}
        self.interpolators = {}
        for spin, spin_data in data.items():
            data_shape = spin_data.shape[2:]
            nbands = self.nbands[spin]
            self.data_shape = data_shape

            # sort the data then reshape them into the grid. The data
            # can now be indexed as data[iband][ikx][iky][ikz]
            sorted_data = spin_data[:, sort_idx]
            grid_shape = (nbands,) + mesh_dim + data_shape
            grid_data = sorted_data.reshape(grid_shape)

            # wrap the data to account for PBC
            pad_size = ((0, 0), (1, 1), (1, 1), (1, 1)) + ((0, 0),) * len(data_shape)
            grid_data = np.pad(grid_data, pad_size, mode="wrap")

            if nbands == 1:
                # this can cause a bug in RegularGridInterpolator. Have to fake
                # having at least two bands
                nbands = 2
                grid_data = np.tile(grid_data, (2, 1, 1, 1) + (1,) * len(data_shape))

            interp_range = (np.arange(nbands), x, y, z)

            self.interpolators[spin] = RegularGridInterpolator(
                interp_range,
                grid_data,
                bounds_error=False,
                fill_value=None,
                # method="nearest"
            )

    @staticmethod
    def _grid_kpoints(kpoints):
        # k-points has to cover the full BZ
        from ifermi.kpoints import get_kpoint_mesh_dim, kpoints_to_first_bz

        kpoints = kpoints_to_first_bz(kpoints)
        mesh_dim = get_kpoint_mesh_dim(kpoints)
        if np.product(mesh_dim) != len(kpoints):
            raise ValueError("k-points do not cover full Brillouin zone.")

        kpoints = np.around(kpoints, 5)

        # get the indices to sort the k-points on the Z, then Y, then X columns
        sort_idx = np.lexsort((kpoints[:, 2], kpoints[:, 1], kpoints[:, 0]))

        # put the kpoints into a 3D grid so that they can be indexed as
        # kpoints[ikx][iky][ikz] = [kx, ky, kz]
        grid_kpoints = kpoints[sort_idx].reshape(mesh_dim + (3,))

        # Expand the k-point mesh to account for periodic boundary conditions
        grid_kpoints = np.pad(
            grid_kpoints, ((1, 1), (1, 1), (1, 1), (0, 0)), mode="wrap"
        )
        grid_kpoints[0, :, :] -= [1, 0, 0]
        grid_kpoints[:, 0, :] -= [0, 1, 0]
        grid_kpoints[:, :, 0] -= [0, 0, 1]
        grid_kpoints[-1, :, :] += [1, 0, 0]
        grid_kpoints[:, -1, :] += [0, 1, 0]
        grid_kpoints[:, :, -1] += [0, 0, 1]
        return grid_kpoints, mesh_dim, sort_idx
