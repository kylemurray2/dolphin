"""stitching.py: utilities for combining interferograms into larger images."""
from __future__ import annotations

import math
import subprocess
import tempfile
from datetime import date
from os import fspath
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

import numpy as np
from numpy.typing import DTypeLike
from osgeo import gdal, osr
from pyproj import Transformer

from dolphin import io, utils
from dolphin._log import get_log
from dolphin._types import Bbox, Filename

logger = get_log(__name__)


def merge_by_date(
    image_file_list: list[Filename],
    file_date_fmt: str = io.DEFAULT_DATETIME_FORMAT,
    output_dir: Filename = ".",
    driver: str = "ENVI",
    output_suffix: str = ".int",
    out_nodata: Optional[Union[float, str]] = 0,
    in_nodata: Optional[Union[float, str]] = None,
    out_bounds: Optional[Bbox] = None,
    out_bounds_epsg: Optional[int] = None,
    options: Optional[Sequence[str]] = io.DEFAULT_ENVI_OPTIONS,
    overwrite: bool = False,
) -> dict[tuple[date, ...], Path]:
    """Group images from the same date and merge into one image per date.

    Parameters
    ----------
    image_file_list : Iterable[Filename]
        list of paths to images.
    file_date_fmt : Optional[str]
        Format of the date in the filename. Default is %Y%m%d
    output_dir : Filename
        Path to output directory
    driver : str
        GDAL driver to use for output. Default is ENVI.
    output_suffix : str
        Suffix to use to output stitched filenames. Default is ".int"
    out_nodata : Optional[float | str]
        Nodata value to use for output file. Default is 0.
    in_nodata : Optional[float | str]
        Override the files' `nodata` and use `in_nodata` during merging.
    out_bounds: Optional[tuple[float]]
        if provided, forces the output image bounds to
            (left, bottom, right, top).
        Otherwise, computes from the outside of all input images.
    out_bounds_epsg: Optional[int]
        EPSG code for the `out_bounds`.
        If not provided, assumed to match the projections of `file_list`.
    options : Optional[Sequence[str]]
        Driver-specific creation options passed to GDAL. Default is ["SUFFIX=ADD"]
    overwrite : bool
        Overwrite existing files. Default is False.

    Returns
    -------
    dict
        key: the date of the SLC acquisitions/date pair of the interferogram.
        value: the path to the stitched image

    Notes
    -----
    This function is intended to be used with filenames that contain date pairs
    (from interferograms).
    """
    grouped_images = utils.group_by_date(image_file_list, file_date_fmt=file_date_fmt)
    stitched_acq_times = {}
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    for dates, cur_images in grouped_images.items():
        logger.info(f"{dates}: Stitching {len(cur_images)} images.")
        if len(dates) == 2:
            date_str = io._format_date_pair(*dates)
        elif len(dates) == 1:
            date_str = dates[0].strftime(file_date_fmt)
        else:
            raise ValueError(f"Expected 1 or 2 dates: {dates}.")
        outfile = Path(output_dir) / (date_str + output_suffix)

        merge_images(
            cur_images,
            outfile=outfile,
            driver=driver,
            overwrite=overwrite,
            out_nodata=out_nodata,
            out_bounds=out_bounds,
            out_bounds_epsg=out_bounds_epsg,
            in_nodata=in_nodata,
            options=options,
        )

        stitched_acq_times[dates] = outfile

    return stitched_acq_times


def merge_images(
    file_list: Sequence[Filename],
    outfile: Filename,
    target_aligned_pixels: bool = True,
    out_bounds: Optional[Bbox] = None,
    out_bounds_epsg: Optional[int] = None,
    strides: dict[str, int] = {"x": 1, "y": 1},
    driver: str = "ENVI",
    out_nodata: Optional[Union[float, str]] = 0,
    out_dtype: Optional[DTypeLike] = None,
    in_nodata: Optional[Union[float, str]] = None,
    resample_alg: str = "lanczos",
    overwrite=False,
    options: Optional[Sequence[str]] = io.DEFAULT_ENVI_OPTIONS,
    create_only: bool = False,
) -> None:
    """Combine multiple SLC images on the same date into one image.

    Parameters
    ----------
    file_list : list[Filename]
        list of raster filenames
    outfile : Filename
        Path to output file
    target_aligned_pixels: bool
        If True, adjust output image bounds so that pixel coordinates
        are integer multiples of pixel size, matching the ``-tap``
        options of GDAL utilities.
        Default is True.
    out_bounds: Optional[tuple[float]]
        if provided, forces the output image bounds to
            (left, bottom, right, top).
        Otherwise, computes from the outside of all input images.
    out_bounds_epsg: Optional[int]
        EPSG code for the `out_bounds`.
        If not provided, assumed to match the projections of `file_list`.
    strides : dict[str, int]
        subsample factor: {"x": x strides, "y": y strides}
    driver : str
        GDAL driver to use for output file. Default is ENVI.
    out_nodata : Optional[float | str]
        Nodata value to use for output file. Default is 0.
    out_dtype : Optional[DTypeLike]
        Output data type. Default is None, which will use the data type
        of the first image in the list.
    in_nodata : Optional[float | str]
        Override the files' `nodata` and use `in_nodata` during merging.
    resample_alg : str, default="lanczos"
        Method for gdal to use for reprojection.
        Default is lanczos (sinc-kernel)
    overwrite : bool
        Overwrite existing files. Default is False.
    options : Optional[Sequence[str]]
        Driver-specific creation options passed to GDAL. Default is ["SUFFIX=ADD"]
    create_only : bool
        If True, creates an empty output file, does not write data. Default is False.
    """
    if Path(outfile).exists():
        if not overwrite:
            logger.info(f"{outfile} already exists, skipping")
            return
        else:
            logger.info(f"Overwrite=True: removing {outfile}")
            Path(outfile).unlink()

    if len(file_list) == 1:
        logger.info("Only one image, no stitching needed")
        logger.info(f"Copying {file_list[0]} to {outfile} and zeroing nodata values.")
        _nodata_to_zero(
            file_list[0],
            outfile=outfile,
            driver=driver,
            creation_options=options,
        )
        return

    # Make sure all the files are in the same projection.
    projection = _get_mode_projection(file_list)
    # If not, warp them to the most common projection using VRT files in a tempdir
    temp_dir = tempfile.TemporaryDirectory()

    if strides is not None and strides["x"] > 1 and strides["y"] > 1:
        file_list = get_downsampled_vrts(
            file_list,
            strides=strides,
            dirname=Path(temp_dir.name),
        )

    warped_file_list = warp_to_projection(
        file_list,
        # temp_dir,
        dirname=Path(temp_dir.name),
        projection=projection,
        resample_alg=resample_alg,
    )
    # Compute output array shape. We guarantee it will cover the output
    # bounds completely
    bounds, combined_nodata = get_combined_bounds_nodata(  # type: ignore
        *warped_file_list,
        target_aligned_pixels=target_aligned_pixels,
        out_bounds=out_bounds,
        out_bounds_epsg=out_bounds_epsg,
        strides=strides,
    )
    (xmin, ymin, xmax, ymax) = bounds

    # Write out the files for gdal_merge using the --optfile flag
    optfile = Path(temp_dir.name) / "file_list.txt"
    optfile.write_text("\n".join(map(str, warped_file_list)))
    args = [
        "gdal_merge.py",
        "-o",
        outfile,
        "--optfile",
        optfile,
        "-of",
        driver,
        "-ul_lr",
        xmin,
        ymax,
        xmax,
        ymin,
    ]
    if out_nodata is not None:
        args.extend(["-a_nodata", str(out_nodata)])
    if in_nodata is not None or combined_nodata is not None:
        ndv = str(in_nodata) if in_nodata is not None else str(combined_nodata)
        args.extend(["-n", ndv])  # type: ignore
    if out_dtype is not None:
        out_gdal_dtype = gdal.GetDataTypeName(io.numpy_to_gdal_type(out_dtype))
        args.extend(["-ot", out_gdal_dtype])
    if target_aligned_pixels:
        args.append("-tap")
    if create_only:
        args.append("-create")
    if options is not None:
        for option in options:
            args.extend(["-co", option])

    arg_list = [str(a) for a in args]
    logger.info(f"Running {' '.join(arg_list)}")
    subprocess.check_call(arg_list)

    temp_dir.cleanup()


def get_downsampled_vrts(
    filenames: Sequence[Filename],
    strides: dict[str, int],
    dirname: Filename,
) -> list[Path]:
    """Create downsampled VRTs from a list of files.

    Does not reproject, only uses `gdal_translate`.


    Parameters
    ----------
    filenames : Sequence[Filename]
        list of filenames to warp.
    strides : dict[str, int]
        subsample factor: {"x": x strides, "y": y strides}
    dirname : Filename
        The directory to write the warped files to.

    Returns
    -------
    list[Filename]
        The warped filenames.
    """
    if not filenames:
        return []
    warped_files = []
    res = _get_resolution(filenames)
    for idx, fn in enumerate(filenames):
        fn = Path(fn)
        warped_fn = Path(dirname) / _get_temp_filename(fn, idx, "_downsampled")
        logger.debug(f"Downsampling {fn} by {strides}")
        warped_files.append(warped_fn)
        gdal.Translate(
            fspath(warped_fn),
            fspath(fn),
            format="VRT",  # Just creates a file that will warp on the fly
            resampleAlg="nearest",  # nearest neighbor for resampling
            xRes=res[0] * strides["x"],
            yRes=res[1] * strides["y"],
        )

    return warped_files


def _get_temp_filename(fn: Path, idx: int, extra: str = ""):
    base = utils._get_path_from_gdal_str(fn).stem
    return f"{base}_{idx}{extra}.vrt"


def warp_to_projection(
    filenames: Sequence[Filename],
    dirname: Filename,
    projection: str,
    res: Optional[tuple[float, float]] = None,
    resample_alg: str = "lanczos",
) -> list[Path]:
    """Warp a list of files to `projection`.

    If the input file's projection matches `projection`, the same file is returned.
    Otherwise, a new file is created in `dirname` with the same name as the input file,
    but with '_warped' appended.

    Parameters
    ----------
    filenames : Sequence[Filename]
        list of filenames to warp.
    dirname : Filename
        The directory to write the warped files to.
    projection : str
        The desired projection, as a WKT string or 'EPSG:XXXX' string.
    res : tuple[float, float]
        The desired [x, y] resolution.
    resample_alg : str, default="lanczos"
        Method for gdal to use for reprojection.
        Default is lanczos (sinc-kernel)

    Returns
    -------
    list[Filename]
        The warped filenames.
    """
    if projection is None:
        projection = _get_mode_projection(filenames)
    if res is None:
        res = _get_resolution(filenames)

    warped_files = []
    for idx, fn in enumerate(filenames):
        fn = Path(fn)
        ds = gdal.Open(fspath(fn))
        proj_in = ds.GetProjection()
        if proj_in == projection:
            warped_files.append(fn)
            continue
        warped_fn = Path(dirname) / _get_temp_filename(fn, idx, "_warped")
        warped_fn = Path(dirname) / f"{fn.stem}_{idx}_warped.vrt"
        from_srs_name = ds.GetSpatialRef().GetName()
        to_srs_name = osr.SpatialReference(projection).GetName()
        logger.info(
            f"Reprojecting {fn} from {from_srs_name} to match mode projection"
            f" {to_srs_name}"
        )
        warped_files.append(warped_fn)
        gdal.Warp(
            fspath(warped_fn),
            fspath(fn),
            format="VRT",  # Just creates a file that will warp on the fly
            dstSRS=projection,
            resampleAlg=resample_alg,
            targetAlignedPixels=True,  # align in multiples of dx, dy
            xRes=res[0],
            yRes=res[1],
        )

    return warped_files


def _get_mode_projection(filenames: Iterable[Filename]) -> str:
    """Get the most common projection in the list."""
    projs = [gdal.Open(fspath(fn)).GetProjection() for fn in filenames]
    return max(set(projs), key=projs.count)


def _get_resolution(filenames: Iterable[Filename]) -> tuple[float, float]:
    """Get the most common resolution in the list."""
    gts = [gdal.Open(fspath(fn)).GetGeoTransform() for fn in filenames]
    res = [(dx, dy) for (_, dx, _, _, _, dy) in gts]
    if len(set(res)) > 1:
        raise ValueError(f"The input files have different resolutions: {res}. ")
    return res[0]


def get_combined_bounds_nodata(
    *filenames: Filename,
    target_aligned_pixels: bool = False,
    out_bounds: Optional[Bbox] = None,
    out_bounds_epsg: Optional[int] = None,
    strides: dict[str, int] = {"x": 1, "y": 1},
) -> tuple[Bbox, Union[str, float, None]]:
    """Get the bounds and nodata of the combined image.

    Parameters
    ----------
    filenames : list[Filename]
        list of filenames to combine
    target_aligned_pixels : bool
        if True, adjust output image bounds so that pixel coordinates
        are integer multiples of pixel size, matching the `-tap` GDAL option.
    out_bounds: Optional[Bbox]
        if provided, forces the output image bounds to
            (left, bottom, right, top).
        Otherwise, computes from the outside of all input images.
    out_bounds_epsg: Optional[int]
        The EPSG of `out_bounds`. If not provided, assumed to be the same
        as the EPSG of all `filenames`.
    strides : dict[str, int]
        subsample factor: {"x": x strides, "y": y strides}

    Returns
    -------
    bounds : Bbox
        (min_x, min_y, max_x, max_y)
    nodata : float
        Nodata value of the input files

    Raises
    ------
    ValueError:
        If the inputs files have different resolutions/projections/nodata values
    """
    # scan input files
    xs = []
    ys = []
    resolutions = set()
    projs = set()
    nodatas = set()

    # Check all files match in resolution/projection
    for fn in filenames:
        ds = gdal.Open(fspath(fn))
        left, bottom, right, top = io.get_raster_bounds(fn)
        gt = ds.GetGeoTransform()
        dx, dy = gt[1], gt[5]

        resolutions.add((abs(dx), abs(dy)))  # dy is negative for north-up
        projs.add(ds.GetProjection())

        xs.extend([left, right])
        ys.extend([bottom, top])

        nd = io.get_raster_nodata(fn)
        # Need to stringify 'nan', or it is repeatedly added
        nodatas.add(str(nd) if (nd is not None and np.isnan(nd)) else nd)

    if len(resolutions) > 1:
        raise ValueError(f"The input files have different resolutions: {resolutions}. ")
    if len(projs) > 1:
        raise ValueError(f"The input files have different projections: {projs}. ")
    if len(nodatas) > 1:
        raise ValueError(f"The input files have different nodata values: {nodatas}. ")
    res = (abs(dx) * strides["x"], abs(dy) * strides["y"])

    if out_bounds is not None:
        if out_bounds_epsg is not None:
            dst_epsg = io.get_raster_crs(filenames[0]).to_epsg()
            bounds = _reproject_bounds(out_bounds, out_bounds_epsg, dst_epsg)
        else:
            bounds = out_bounds  # type: ignore
    else:
        bounds = min(xs), min(ys), max(xs), max(ys)

    if target_aligned_pixels:
        bounds = _align_bounds(bounds, res)

    return bounds, list(nodatas)[0]


def _align_bounds(bounds: Iterable[float], res: tuple[float, float]):
    """Align boundary with an integer multiple of the resolution."""
    left, bottom, right, top = bounds
    left = math.floor(left / res[0]) * res[0]
    right = math.ceil(right / res[0]) * res[0]
    bottom = math.floor(bottom / res[1]) * res[1]
    top = math.ceil(top / res[1]) * res[1]
    return (left, bottom, right, top)


def _reproject_bounds(bounds: Bbox, src_epsg: int, dst_epsg: int) -> Bbox:
    t = Transformer.from_crs(src_epsg, dst_epsg, always_xy=True)
    left, bottom, right, top = bounds
    bbox: Bbox = (*t.transform(left, bottom), *t.transform(right, top))  # type: ignore
    return bbox


def _nodata_to_zero(
    infile: Filename,
    outfile: Optional[Filename] = None,
    ext: Optional[str] = None,
    in_band: int = 1,
    driver="ENVI",
    creation_options=io.DEFAULT_ENVI_OPTIONS,
):
    """Make a copy of infile and replace NaNs with 0."""
    in_p = Path(infile)
    if outfile is None:
        if ext is None:
            ext = in_p.suffix
        out_dir = in_p.parent
        outfile = out_dir / (in_p.stem + "_tmp" + ext)

    ds_in = gdal.Open(fspath(infile))
    drv = gdal.GetDriverByName(driver)
    ds_out = drv.CreateCopy(fspath(outfile), ds_in, options=creation_options)

    bnd = ds_in.GetRasterBand(in_band)
    nodata = bnd.GetNoDataValue()
    arr = bnd.ReadAsArray()
    # also make sure to replace NaNs, even if nodata is not set
    mask = np.logical_or(np.isnan(arr), arr == nodata)
    arr[mask] = 0

    ds_out.GetRasterBand(1).WriteArray(arr)
    ds_out = None

    return outfile


def warp_to_match(
    input_file: Filename,
    match_file: Filename,
    output_file: Optional[Filename] = None,
    resample_alg: str = "near",
    output_format: Optional[str] = None,
) -> Path:
    """Reproject `input_file` to align with the `match_file`.

    Uses the bounds, resolution, and CRS of `match_file`.

    Parameters
    ----------
    input_file: Filename
        Path to the image to be reprojected.
    match_file: Filename
        Path to the input image to serve as a reference for the reprojected image.
        Uses the bounds, resolution, and CRS of this image.
    output_file: Filename
        Path to the output, reprojected image.
        If None, creates an in-memory warped VRT using the `/vsimem/` protocol.
    resample_alg: str, optional, default = "near"
        Resampling algorithm to be used during reprojection.
        See https://gdal.org/programs/gdalwarp.html#cmdoption-gdalwarp-r for choices.
    output_format: str, optional, default = None
        Output format to be used for the output image.
        If None, gdal will try to infer the format from the output file extension, or
        (if the extension of `output_file` matches `input_file`) use the input driver.

    Returns
    -------
    Path
        Path to the output image.
        Same as `output_file` if provided, otherwise a path to the in-memory VRT.
    """
    bounds = io.get_raster_bounds(match_file)
    crs_wkt = io.get_raster_crs(match_file).to_wkt()
    gt = io.get_raster_gt(match_file)
    resolution = (gt[1], gt[5])

    if output_file is None:
        output_file = f"/vsimem/warped_{Path(input_file).stem}.vrt"
        logger.debug(f"Creating in-memory warped VRT: {output_file}")

    if output_format is None and Path(input_file).suffix == Path(output_file).suffix:
        output_format = io.get_raster_driver(input_file)

    options = gdal.WarpOptions(
        dstSRS=crs_wkt,
        format=output_format,
        xRes=resolution[0],
        yRes=resolution[1],
        outputBounds=bounds,
        outputBoundsSRS=crs_wkt,
        resampleAlg=resample_alg,
    )
    gdal.Warp(
        fspath(output_file),
        fspath(input_file),
        options=options,
    )

    return Path(output_file)
