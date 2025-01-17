"""Functions for reading from and writing to raster files.

This module heavily relies on GDAL and provides many convenience/
wrapper functions to write/iterate over blocks of large raster files.
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from os import fspath
from pathlib import Path
from typing import Any, Generator, Optional, Sequence, Union

import h5py
import numpy as np
from numpy.typing import ArrayLike, DTypeLike
from osgeo import gdal
from pyproj import CRS

from dolphin._background import _DEFAULT_TIMEOUT, BackgroundReader, BackgroundWriter
from dolphin._blocks import compute_out_shape, iter_blocks
from dolphin._log import get_log
from dolphin._types import Bbox, Filename
from dolphin.utils import gdal_to_numpy_type, numpy_to_gdal_type, progress

gdal.UseExceptions()

__all__ = [
    "load_gdal",
    "write_arr",
    "write_block",
    "EagerLoader",
]


DEFAULT_TILE_SIZE = [128, 128]
DEFAULT_TIFF_OPTIONS = (
    "COMPRESS=DEFLATE",
    "ZLEVEL=4",
    "TILED=YES",
    f"BLOCKXSIZE={DEFAULT_TILE_SIZE[1]}",
    f"BLOCKYSIZE={DEFAULT_TILE_SIZE[0]}",
)
DEFAULT_ENVI_OPTIONS = ("SUFFIX=ADD",)
DEFAULT_HDF5_OPTIONS = dict(
    # https://docs.h5py.org/en/stable/high/dataset.html#filter-pipeline
    chunks=DEFAULT_TILE_SIZE,
    compression="gzip",
    compression_opts=4,
    shuffle=True,
)
DEFAULT_DATETIME_FORMAT = "%Y%m%d"

logger = get_log(__name__)


def load_gdal(
    filename: Filename,
    *,
    band: Optional[int] = None,
    subsample_factor: Union[int, tuple[int, int]] = 1,
    rows: Optional[slice] = None,
    cols: Optional[slice] = None,
    masked: bool = False,
):
    """Load a gdal file into a numpy array.

    Parameters
    ----------
    filename : str or Path
        Path to the file to load.
    band : int, optional
        Band to load. If None, load all bands as 3D array.
    subsample_factor : int or tuple[int, int], optional
        Subsample the data by this factor. Default is 1 (no subsampling).
        Uses nearest neighbor resampling.
    rows : slice, optional
        Rows to load. Default is None (load all rows).
    cols : slice, optional
        Columns to load. Default is None (load all columns).
    masked : bool, optional
        If True, return a masked array using the raster's `nodata` value.
        Default is False.

    Returns
    -------
    arr : np.ndarray
        Array of shape (bands, y, x) or (y, x) if `band` is specified,
        where y = height // subsample_factor and x = width // subsample_factor.
    """
    ds = gdal.Open(fspath(filename))
    nrows, ncols = ds.RasterYSize, ds.RasterXSize

    # if rows or cols are not specified, load all rows/cols
    rows = slice(0, nrows) if rows in (None, slice(None)) else rows
    cols = slice(0, ncols) if cols in (None, slice(None)) else cols
    # Help out mypy:
    assert rows is not None
    assert cols is not None

    dt = gdal_to_numpy_type(ds.GetRasterBand(1).DataType)

    if isinstance(subsample_factor, int):
        subsample_factor = (subsample_factor, subsample_factor)

    xoff, yoff = int(cols.start), int(rows.start)
    row_stop = min(rows.stop, nrows)
    col_stop = min(cols.stop, ncols)
    xsize, ysize = int(col_stop - cols.start), int(row_stop - rows.start)
    if xsize <= 0 or ysize <= 0:
        raise IndexError(
            f"Invalid row/col slices: {rows}, {cols} for file {filename} of size"
            f" {nrows}x{ncols}"
        )
    nrows_out, ncols_out = (
        ysize // subsample_factor[0],
        xsize // subsample_factor[1],
    )

    # Read the data, and decimate if specified
    resamp = gdal.GRA_NearestNeighbour
    if band is None:
        count = ds.RasterCount
        out = np.empty((count, nrows_out, ncols_out), dtype=dt)
        ds.ReadAsArray(xoff, yoff, xsize, ysize, buf_obj=out, resample_alg=resamp)
        if count == 1:
            out = out[0]
    else:
        out = np.empty((nrows_out, ncols_out), dtype=dt)
        bnd = ds.GetRasterBand(band)
        bnd.ReadAsArray(xoff, yoff, xsize, ysize, buf_obj=out, resample_alg=resamp)

    if not masked:
        return out
    # Get the nodata value
    nd = get_raster_nodata(filename)
    if nd is not None and np.isnan(nd):
        return np.ma.masked_invalid(out)
    else:
        return np.ma.masked_equal(out, nd)


def format_nc_filename(filename: Filename, ds_name: Optional[str] = None) -> str:
    """Format an HDF5/NetCDF filename with dataset for reading using GDAL.

    If `filename` is already formatted, or if `filename` is not an HDF5/NetCDF
    file (based on the file extension), it is returned unchanged.

    Parameters
    ----------
    filename : str or PathLike
        Filename to format.
    ds_name : str, optional
        Dataset name to use. If not provided for a .h5 or .nc file, an error is raised.

    Returns
    -------
    str
        Formatted filename.

    Raises
    ------
    ValueError
        If `ds_name` is not provided for a .h5 or .nc file.
    """
    # If we've already formatted the filename, return it
    if str(filename).startswith("NETCDF:") or str(filename).startswith("HDF5:"):
        return str(filename)

    if not (fspath(filename).endswith(".nc") or fspath(filename).endswith(".h5")):
        return fspath(filename)

    # Now we're definitely dealing with an HDF5/NetCDF file
    if ds_name is None:
        raise ValueError("Must provide dataset name for HDF5/NetCDF files")

    return f'NETCDF:"{filename}":"//{ds_name.lstrip("/")}"'


def _assert_images_same_size(files):
    """Ensure all files are the same size."""
    with ThreadPoolExecutor(5) as executor:
        sizes = list(executor.map(get_raster_xysize, files))
    if len(set(sizes)) > 1:
        raise ValueError(f"Not files have same raster (x, y) size:\n{set(sizes)}")


def copy_projection(src_file: Filename, dst_file: Filename) -> None:
    """Copy projection/geotransform from `src_file` to `dst_file`."""
    ds_src = gdal.Open(fspath(src_file))
    projection = ds_src.GetProjection()
    geotransform = ds_src.GetGeoTransform()
    nodata = ds_src.GetRasterBand(1).GetNoDataValue()

    if projection is None and geotransform is None:
        logger.info("No projection or geotransform found on file %s", input)
        return
    ds_dst = gdal.Open(fspath(dst_file), gdal.GA_Update)

    if geotransform is not None and geotransform != (0, 1, 0, 0, 0, 1):
        ds_dst.SetGeoTransform(geotransform)

    if projection is not None and projection != "":
        ds_dst.SetProjection(projection)

    if nodata is not None:
        ds_dst.GetRasterBand(1).SetNoDataValue(nodata)

    ds_src = ds_dst = None


def get_raster_xysize(filename: Filename) -> tuple[int, int]:
    """Get the xsize/ysize of a GDAL-readable raster."""
    ds = gdal.Open(fspath(filename))
    xsize, ysize = ds.RasterXSize, ds.RasterYSize
    ds = None
    return xsize, ysize


def get_raster_nodata(filename: Filename, band: int = 1) -> Optional[float]:
    """Get the nodata value from a file.

    Parameters
    ----------
    filename : Filename
        Path to the file to load.
    band : int, optional
        Band to get nodata value for, by default 1.

    Returns
    -------
    Optional[float]
        Nodata value, or None if not found.
    """
    ds = gdal.Open(fspath(filename))
    nodata = ds.GetRasterBand(band).GetNoDataValue()
    return nodata


def get_raster_crs(filename: Filename) -> CRS:
    """Get the CRS from a file.

    Parameters
    ----------
    filename : Filename
        Path to the file to load.

    Returns
    -------
    CRS
        CRS.
    """
    ds = gdal.Open(fspath(filename))
    crs = CRS.from_wkt(ds.GetProjection())
    return crs


def get_raster_gt(filename: Filename) -> list[float]:
    """Get the geotransform from a file.

    Parameters
    ----------
    filename : Filename
        Path to the file to load.

    Returns
    -------
    List[float]
        6 floats representing a GDAL Geotransform.
    """
    ds = gdal.Open(fspath(filename))
    gt = ds.GetGeoTransform()
    return gt


def get_raster_dtype(filename: Filename) -> np.dtype:
    """Get the data type from a file.

    Parameters
    ----------
    filename : Filename
        Path to the file to load.

    Returns
    -------
    np.dtype
        Data type.
    """
    ds = gdal.Open(fspath(filename))
    dt = gdal_to_numpy_type(ds.GetRasterBand(1).DataType)
    return dt


def get_raster_driver(filename: Filename) -> str:
    """Get the GDAL driver `ShortName` from a file.

    Parameters
    ----------
    filename : Filename
        Path to the file to load.

    Returns
    -------
    str
        Driver name.
    """
    ds = gdal.Open(fspath(filename))
    driver = ds.GetDriver().ShortName
    return driver


def get_raster_bounds(
    filename: Optional[Filename] = None, ds: Optional[gdal.Dataset] = None
) -> Bbox:
    """Get the (left, bottom, right, top) bounds of the image."""
    if ds is None:
        if filename is None:
            raise ValueError("Must provide either `filename` or `ds`")
        ds = gdal.Open(fspath(filename))

    gt = ds.GetGeoTransform()
    xsize, ysize = ds.RasterXSize, ds.RasterYSize

    left, top = _apply_gt(gt=gt, x=0, y=0)
    right, bottom = _apply_gt(gt=gt, x=xsize, y=ysize)

    return (left, bottom, right, top)


def rowcol_to_xy(
    row: int,
    col: int,
    ds: Optional[gdal.Dataset] = None,
    filename: Optional[Filename] = None,
) -> tuple[float, float]:
    """Convert indexes in the image space to georeferenced coordinates."""
    return _apply_gt(ds, filename, col, row)


def xy_to_rowcol(
    x: float,
    y: float,
    ds: Optional[gdal.Dataset] = None,
    filename: Optional[Filename] = None,
    do_round=True,
) -> tuple[int, int]:
    """Convert coordinates in the georeferenced space to a row and column index."""
    col, row = _apply_gt(ds, filename, x, y, inverse=True)
    # Need to convert to int, otherwise we get a float
    if do_round:
        # round up to the nearest pixel, instead of banker's rounding
        row = int(math.floor(row + 0.5))
        col = int(math.floor(col + 0.5))
    return int(row), int(col)


def _apply_gt(
    ds=None, filename=None, x=None, y=None, inverse=False, gt=None
) -> tuple[float, float]:
    """Read the (possibly inverse) geotransform, apply to the x/y coordinates."""
    if gt is None:
        if ds is None:
            ds = gdal.Open(fspath(filename))
            gt = ds.GetGeoTransform()
            ds = None
        else:
            gt = ds.GetGeoTransform()

    if inverse:
        gt = gdal.InvGeoTransform(gt)
    # Reference: https://gdal.org/tutorials/geotransforms_tut.html
    x = gt[0] + x * gt[1] + y * gt[2]
    y = gt[3] + x * gt[4] + y * gt[5]
    return x, y


def write_arr(
    *,
    arr: Optional[ArrayLike],
    output_name: Filename,
    like_filename: Optional[Filename] = None,
    driver: Optional[str] = "GTiff",
    options: Optional[Sequence] = None,
    nbands: Optional[int] = None,
    shape: Optional[tuple[int, int]] = None,
    dtype: Optional[DTypeLike] = None,
    geotransform: Optional[Sequence[float]] = None,
    strides: Optional[dict[str, int]] = None,
    projection: Optional[Any] = None,
    nodata: Optional[Union[float, str]] = None,
):
    """Save an array to `output_name`.

    If `like_filename` if provided, copies the projection/nodata.
    Options can be overridden by passing `driver`/`nbands`/`dtype`.

    If arr is None, create an empty file with the same x/y shape as `like_filename`.

    Parameters
    ----------
    arr : ArrayLike, optional
        Array to save. If None, create an empty file.
    output_name : str or Path
        Path to save the file to.
    like_filename : str or Path, optional
        Path to a file to copy raster shape/metadata from.
    driver : str, optional
        GDAL driver to use. Default is "GTiff".
    options : list, optional
        list of options to pass to the driver. Default is DEFAULT_TIFF_OPTIONS.
    nbands : int, optional
        Number of bands to save. Default is 1.
    shape : tuple, optional
        (rows, cols) of desired output file.
        Overrides the shape of the output file, if using `like_filename`.
    dtype : DTypeLike, optional
        Data type to save. Default is `arr.dtype` or the datatype of like_filename.
    geotransform : list, optional
        Geotransform to save. Default is the geotransform of like_filename.
        See https://gdal.org/tutorials/geotransforms_tut.html .
    strides : dict, optional
        If using `like_filename`, used to change the pixel size of the output file.
        {"x": x strides, "y": y strides}
    projection : str or int, optional
        Projection to save. Default is the projection of like_filename.
        Possible values are anything parse-able by ``pyproj.CRS.from_user_input``
        (including EPSG ints, WKT strings, PROJ strings, etc.)
    nodata : float or str, optional
        Nodata value to save.
        Default is the nodata of band 1 of `like_filename` (if provided), or None.

    """
    fi = FileInfo.from_user_inputs(
        arr=arr,
        output_name=output_name,
        like_filename=like_filename,
        driver=driver,
        options=options,
        nbands=nbands,
        shape=shape,
        dtype=dtype,
        geotransform=geotransform,
        strides=strides,
        projection=projection,
        nodata=nodata,
    )
    drv = gdal.GetDriverByName(fi.driver)
    ds_out = drv.Create(
        fspath(output_name),
        fi.xsize,
        fi.ysize,
        fi.nbands,
        fi.gdal_dtype,
        options=fi.options,
    )

    # Set the geo/proj information
    if fi.projection:
        # Make sure we're got a correct format for the projection
        # this still works if we're passed a WKT string
        proj = CRS.from_user_input(fi.projection).to_wkt()
        ds_out.SetProjection(proj)

    if fi.geotransform is not None:
        ds_out.SetGeoTransform(fi.geotransform)

    # Write the actual data
    if arr is not None:
        if arr.ndim == 2:
            arr = arr[np.newaxis, ...]
        for i in range(fi.nbands):
            logger.debug(f"Writing band {i+1}/{fi.nbands}")
            bnd = ds_out.GetRasterBand(i + 1)
            bnd.WriteArray(arr[i])

    # Set the nodata value for each band
    if fi.nodata is not None:
        for i in range(fi.nbands):
            logger.debug(f"Setting nodata for band {i+1}/{fi.nbands}")
            bnd = ds_out.GetRasterBand(i + 1)
            bnd.SetNoDataValue(fi.nodata)

    ds_out.FlushCache()
    ds_out = None


def write_block(
    cur_block: ArrayLike,
    filename: Filename,
    row_start: int,
    col_start: int,
):
    """Write out an ndarray to a subset of the pre-made `filename`.

    Parameters
    ----------
    cur_block : ArrayLike
        2D or 3D data array
    filename : Filename
        list of output files to save to, or (if cur_block is 2D) a single file.
    row_start : int
        Row index to start writing at.
    col_start : int
        Column index to start writing at.

    Raises
    ------
    ValueError
        If length of `output_files` does not match length of `cur_block`.
    """
    if cur_block.ndim == 2:
        # Make into 3D array shaped (1, rows, cols)
        cur_block = cur_block[np.newaxis, ...]
    # filename must be pre-made
    filename = Path(filename)
    if not filename.exists():
        raise ValueError(f"File {filename} does not exist")

    if filename.suffix in (".h5", ".hdf5", ".nc"):
        _write_hdf5(cur_block, filename, row_start, col_start)
    else:
        _write_gdal(cur_block, filename, row_start, col_start)


def _write_gdal(
    cur_block: ArrayLike,
    filename: Filename,
    row_start: int,
    col_start: int,
):
    ds = gdal.Open(fspath(filename), gdal.GA_Update)
    for b_idx, cur_image in enumerate(cur_block, start=1):
        bnd = ds.GetRasterBand(b_idx)
        # only need offset for write:
        # https://gdal.org/api/python/osgeo.gdal.html#osgeo.gdal.Band.WriteArray
        bnd.WriteArray(cur_image, col_start, row_start)
        bnd.FlushCache()
        bnd = None
    ds = None


def _write_hdf5(
    cur_block: ArrayLike,
    filename: Filename,
    row_start: int,
    col_start: int,
):
    nrows, ncols = cur_block.shape[-2:]
    row_slice = slice(row_start, row_start + nrows)
    col_slice = slice(col_start, col_start + ncols)
    with h5py.File(filename, "a") as hf:
        hf[row_slice, col_slice] = cur_block


@dataclass
class FileInfo:
    nbands: int
    ysize: int
    xsize: int
    dtype: DTypeLike
    gdal_dtype: int
    nodata: Optional[Union[str, float]]
    driver: str
    options: Optional[list]
    projection: Optional[str]
    geotransform: Optional[list[float]]

    @classmethod
    def from_user_inputs(
        cls,
        *,
        arr: Optional[ArrayLike],
        output_name: Filename,
        like_filename: Optional[Filename] = None,
        driver: Optional[str] = "GTiff",
        options: Optional[Sequence[Any]] = [],
        nbands: Optional[int] = None,
        shape: Optional[tuple[int, int]] = None,
        dtype: Optional[DTypeLike] = None,
        geotransform: Optional[Sequence[float]] = None,
        strides: Optional[dict[str, int]] = None,
        projection: Optional[Any] = None,
        nodata: Optional[Union[float, str]] = None,
    ) -> FileInfo:
        if like_filename is not None:
            ds_like = gdal.Open(fspath(like_filename))
        else:
            ds_like = None

        xsize = ysize = gdal_dtype = None
        if arr is not None:
            if arr.ndim == 2:
                arr = arr[np.newaxis, ...]
            ysize, xsize = arr.shape[-2:]
            gdal_dtype = numpy_to_gdal_type(arr.dtype)
        else:
            # If not passing an array to write, get shape/dtype from like_filename
            if shape is not None:
                ysize, xsize = shape
            else:
                xsize, ysize = ds_like.RasterXSize, ds_like.RasterYSize
                # If using strides, adjust the output shape
                if strides is not None:
                    ysize, xsize = compute_out_shape((ysize, xsize), strides)

            if dtype is not None:
                gdal_dtype = numpy_to_gdal_type(dtype)
            else:
                gdal_dtype = ds_like.GetRasterBand(1).DataType

        if any(v is None for v in (xsize, ysize, gdal_dtype)):
            raise ValueError("Must specify either `arr` or `like_filename`")
        assert gdal_dtype is not None

        if nodata is None and ds_like is not None:
            b = ds_like.GetRasterBand(1)
            nodata = b.GetNoDataValue()

        if nbands is None:
            if arr is not None:
                nbands = arr.shape[0]
            elif ds_like is not None:
                nbands = ds_like.RasterCount
            else:
                nbands = 1

        if driver is None:
            if str(output_name).endswith(".tif"):
                driver = "GTiff"
            else:
                if not ds_like:
                    raise ValueError("Must specify `driver` if `like_filename` is None")
                driver = ds_like.GetDriver().ShortName
        if options is None and driver == "GTiff":
            options = list(DEFAULT_TIFF_OPTIONS)
        if not options:
            options = []

        # If not provided, attempt to get projection/geotransform from like_filename
        if projection is None and ds_like is not None:
            projection = ds_like.GetProjection()
        if geotransform is None and ds_like is not None:
            geotransform = ds_like.GetGeoTransform()
            # If we're using strides, adjust the geotransform
            if strides is not None:
                geotransform = list(geotransform)
                geotransform[1] *= strides["x"]
                geotransform[5] *= strides["y"]

        return cls(
            nbands=nbands,
            ysize=ysize,
            xsize=xsize,
            dtype=dtype,
            gdal_dtype=gdal_dtype,
            nodata=nodata,
            driver=driver,
            options=list(options),
            projection=projection,
            geotransform=list(geotransform) if geotransform else None,
        )


class Writer(BackgroundWriter):
    """Class to write data to files in a background thread."""

    def __init__(self, max_queue: int = 0, debug: bool = False, **kwargs):
        if debug is False:
            super().__init__(nq=max_queue, name="Writer", **kwargs)
        else:
            # Don't start a background thread. Just synchronously write data
            self.queue_write = lambda *args: write_block(*args)  # type: ignore

    def write(
        self, data: ArrayLike, filename: Filename, row_start: int, col_start: int
    ):
        """Write out an ndarray to a subset of the pre-made `filename`.

        Parameters
        ----------
        data : ArrayLike
            2D or 3D data array to save.
        filename : Filename
            list of output files to save to, or (if cur_block is 2D) a single file.
        row_start : int
            Row index to start writing at.
        col_start : int
            Column index to start writing at.

        Raises
        ------
        ValueError
            If length of `output_files` does not match length of `cur_block`.
        """
        write_block(data, filename, row_start, col_start)

    @property
    def num_queued(self):
        """Number of items waiting in the queue to be written."""
        return self._work_queue.qsize()


class EagerLoader(BackgroundReader):
    """Class to pre-fetch data chunks in a background thread."""

    def __init__(
        self,
        filename: Filename,
        block_shape: tuple[int, int],
        overlaps: tuple[int, int] = (0, 0),
        skip_empty: bool = True,
        nodata_mask: Optional[ArrayLike] = None,
        queue_size: int = 1,
        timeout: float = _DEFAULT_TIMEOUT,
        show_progress: bool = True,
    ):
        super().__init__(nq=queue_size, timeout=timeout, name="EagerLoader")
        self.filename = filename
        # Set up the generator of ((row_start, row_end), (col_start, col_end))
        xsize, ysize = get_raster_xysize(filename)
        # convert the slice generator to a list so we have the size
        self.slices = list(
            iter_blocks(
                arr_shape=(ysize, xsize),
                block_shape=block_shape,
                overlaps=overlaps,
            )
        )
        self._queue_size = queue_size
        self._skip_empty = skip_empty
        self._nodata_mask = nodata_mask
        self._block_shape = block_shape
        self._nodata = get_raster_nodata(filename)
        self._show_progress = show_progress
        if self._nodata is None:
            self._nodata = np.nan

    def read(self, rows: slice, cols: slice) -> tuple[np.ndarray, tuple[slice, slice]]:
        logger.debug(f"EagerLoader reading {rows}, {cols}")
        cur_block = load_gdal(self.filename, rows=rows, cols=cols)
        return cur_block, (rows, cols)

    def iter_blocks(
        self,
    ) -> Generator[tuple[np.ndarray, tuple[slice, slice]], None, None]:
        # Queue up all slices to the work queue
        queued_slices = []
        for rows, cols in self.slices:
            # Skip queueing a read if all nodata
            if self._skip_empty and self._nodata_mask is not None:
                logger.debug("Checking nodata mask")
                if self._nodata_mask[rows, cols].all():
                    logger.debug("Skipping!")
                    continue
            self.queue_read(rows, cols)
            queued_slices.append((rows, cols))

        s_iter = range(len(queued_slices))
        desc = f"Processing {self._block_shape} sized blocks..."
        with progress(dummy=not self._show_progress) as p:
            for _ in p.track(s_iter, description=desc):
                cur_block, (rows, cols) = self.get_data()
                logger.debug(f"got data for {rows, cols}: {cur_block.shape}")

                # Otherwise look at the actual block we loaded
                if np.isnan(self._nodata):
                    block_nodata = np.isnan(cur_block)
                else:
                    block_nodata = cur_block == self._nodata
                if np.all(block_nodata):
                    logger.debug("Skipping block since it was all nodata")
                    continue
                yield cur_block, (rows, cols)

        self.notify_finished()


def get_max_block_shape(
    filename: Filename, nstack: int, max_bytes: float = 64e6
) -> tuple[int, int]:
    """Find a block shape to load from `filename` with memory size < `max_bytes`.

    Attempts to get an integer number of chunks ("tiles" for geotiffs) from the
    file to avoid partial tiles.

    Parameters
    ----------
    filename : str
        GDAL-readable file name containing 3D dataset.
    nstack: int
        Number of bands in dataset.
    max_bytes : float, optional
        Target size of memory (in Bytes) for each block.
        Defaults to 64e6.

    Returns
    -------
    tuple[int, int]:
        (num_rows, num_cols) shape of blocks to load from `vrt_file`
    """
    chunk_cols, chunk_rows = get_raster_chunk_size(filename)
    xsize, ysize = get_raster_xysize(filename)
    # If it's written by line, load at least 16 lines at a time
    chunk_cols = min(max(16, chunk_cols), xsize)
    chunk_rows = min(max(16, chunk_rows), ysize)

    ds = gdal.Open(fspath(filename))
    shape = (ds.RasterYSize, ds.RasterXSize)
    # get the size of the data type from the raster
    nbytes = gdal_to_numpy_type(ds.GetRasterBand(1).DataType).itemsize
    return _increment_until_max(
        max_bytes=max_bytes,
        file_chunk_size=[chunk_rows, chunk_cols],
        shape=shape,
        nstack=nstack,
        bytes_per_pixel=nbytes,
    )


def get_raster_chunk_size(filename: Filename) -> list[int]:
    """Get size the raster's chunks on disk.

    This is called blockXsize, blockYsize by GDAL.
    """
    ds = gdal.Open(fspath(filename))
    block_size = ds.GetRasterBand(1).GetBlockSize()
    for i in range(2, ds.RasterCount + 1):
        if block_size != ds.GetRasterBand(i).GetBlockSize():
            logger.warning(f"Warning: {filename} bands have different block shapes.")
            break
    return block_size


def _format_date_pair(start: date, end: date, fmt=DEFAULT_DATETIME_FORMAT) -> str:
    return f"{start.strftime(fmt)}_{end.strftime(fmt)}"


def _increment_until_max(
    max_bytes: float,
    file_chunk_size: Sequence[int],
    shape: tuple[int, int],
    nstack: int,
    bytes_per_pixel: int = 8,
) -> tuple[int, int]:
    """Find size of 3D chunk to load while staying at ~`max_bytes` bytes of RAM."""
    chunk_rows, chunk_cols = file_chunk_size

    # How many chunks can we fit in max_bytes?
    chunks_per_block = max_bytes / (
        (nstack * chunk_rows * chunk_cols) * bytes_per_pixel
    )
    num_chunks = [1, 1]
    cur_block_shape = [chunk_rows, chunk_cols]

    idx = 1  # start incrementing cols
    while chunks_per_block > 1 and tuple(cur_block_shape) != tuple(shape):
        # Alternate between adding a row and column chunk by flipping the idx
        chunk_idx = idx % 2
        nc = num_chunks[chunk_idx]
        chunk_size = file_chunk_size[chunk_idx]

        cur_block_shape[chunk_idx] = min(nc * chunk_size, shape[chunk_idx])

        chunks_per_block = max_bytes / (
            nstack * np.prod(cur_block_shape) * bytes_per_pixel
        )
        num_chunks[chunk_idx] += 1
        idx += 1
    return cur_block_shape[0], cur_block_shape[1]
