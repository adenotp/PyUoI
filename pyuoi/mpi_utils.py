"""
Helper functions for working with MPI.
"""
import h5py
import numpy as np

from mpi4py import MPI
_np2mpi = {np.dtype(np.float32): MPI.FLOAT,
           np.dtype(np.float64): MPI.DOUBLE,
           np.dtype(np.int): MPI.LONG,
           np.dtype(np.intc): MPI.INT}


def load_data_MPI(h5_name, X_key='X', y_key='y', root=0):
    """Load data from an h5 file and broadcast it across MPI ranks.

    This is a helper function. It is also possible to load the data
    without this function.

    Parameters
    ----------
    h5_name : str
        Path to h5 file.
    X_key : str
        Key for the features dataset. (default: 'X')
    y_key : str
        Key for the targets dataset. (default: 'y')

    Returns
    -------
    X : ndarray
        Features on all MPI ranks.
    y : ndarray
        Targets on all MPI ranks.
    """

    comm = MPI.COMM_WORLD
    rank = comm.rank
    Xshape = None
    Xdtype = None
    yshape = None
    ydtype = None
    if rank == root:
        with h5py.File(h5_name, 'r') as f:
            X = f[X_key][()]
            Xshape = X.shape
            Xdtype = X.dtype
            y = f[y_key][()]
            yshape = y.shape
            ydtype = y.dtype
    Xshape = comm.bcast(Xshape, root=root)
    Xdtype = comm.bcast(Xdtype, root=root)
    yshape = comm.bcast(yshape, root=root)
    ydtype = comm.bcast(ydtype, root=root)
    if rank != root:
        X = np.empty(Xshape, dtype=Xdtype)
        y = np.empty(yshape, dtype=ydtype)
    comm.Bcast([X, _np2mpi[np.dtype(X.dtype)]], root=root)
    comm.Bcast([y, _np2mpi[np.dtype(y.dtype)]], root=root)
    return X, y


def Bcast_from_root(send, comm, root=0):
    """Broadcast an array from root to all MPI ranks.

    Parameters
    ----------
    send : ndarray or None
        Array to send from root to all ranks. send in other ranks
        has no effect.
    comm : MPI.COMM_WORLD
        MPI communicator.
    root : int, default 0
        This rank contains the array to send.

    Returns
    -------
    send : ndarray
        Each rank will have a copy of the array from root.
    """
    rank = comm.rank
    if rank == 0:
        dtype = send.dtype
        shape = send.shape
    else:
        dtype = None
        shape = None
    shape = comm.bcast(shape, root=root)
    dtype = comm.bcast(dtype, root=root)
    if rank != 0:
        send = np.empty(shape, dtype=dtype)
    comm.Bcast([send, _np2mpi[np.dtype(dtype)]], root=root)
    return send


def Gatherv_rows(send, comm, root=0):
    """Concatenate arrays along the first axis using Gatherv.

    Parameters
    ----------
    send : ndarray
        The arrays to concatenate. All dimensions must be equal except for the
        first.
    comm : MPI.COMM_WORLD
        MPI communicator.
    root : int, default 0
        This rank will contain the Gatherv'ed array.

    Returns
    -------
    rec : ndarray or None
        Gatherv'ed array on root or None on other ranks.
    """

    rank = comm.rank
    size = comm.size
    dtype = send.dtype
    shape = send.shape
    tot = np.zeros(1, dtype=int)
    comm.Reduce(np.array(shape[0], dtype=int),
                [tot, _np2mpi[tot.dtype]], op=MPI.SUM, root=root)
    if rank == root:
        rec_shape = (tot[0],) + shape[1:]
        rec = np.empty(rec_shape, dtype=dtype)
        idxs = np.array_split(np.arange(rec_shape[0]), size)
        sizes = [idx.size * np.prod(rec_shape[1:]) for idx in idxs]
        disps = np.insert(np.cumsum(sizes), 0, 0)[:-1]
    else:
        rec = None
        idxs = None
        sizes = None
        disps = None

    comm.Gatherv(send, [rec, sizes, disps, _np2mpi[dtype]], root=0)
    return rec
