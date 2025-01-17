"""Find the persistent scatterers in a stack of SLCS."""
from __future__ import annotations

import shutil
import warnings
from collections import namedtuple
from pathlib import Path
from typing import Optional

import numpy as np
from numpy.typing import ArrayLike
from osgeo import gdal

from dolphin import io, utils
from dolphin._log import get_log
from dolphin._types import Filename
from dolphin.stack import VRTStack

gdal.UseExceptions()

logger = get_log(__name__)

PsFileOptions = namedtuple("PsFileOptions", ["ps", "amp_dispersion", "amp_mean"])
NODATA_VALUES = PsFileOptions(255, 0, 0)
FILE_DTYPES = PsFileOptions(np.uint8, np.float32, np.float32)


def create_ps(
    *,
    slc_vrt_file: Filename,
    output_file: Filename,
    output_amp_mean_file: Filename,
    output_amp_dispersion_file: Filename,
    amp_dispersion_threshold: float = 0.25,
    existing_amp_mean_file: Optional[Filename] = None,
    existing_amp_dispersion_file: Optional[Filename] = None,
    nodata_mask: Optional[np.ndarray] = None,
    update_existing: bool = False,
    block_shape: tuple[int, int] = (512, 512),
    show_progress: bool = True,
):
    """Create the amplitude dispersion, mean, and PS files.

    Parameters
    ----------
    slc_vrt_file : Filename
        The VRT file pointing to the stack of SLCs.
    output_file : Filename
        The output PS file (dtype: Byte)
    output_amp_dispersion_file : Filename
        The output amplitude dispersion file.
    output_amp_mean_file : Filename
        The output mean amplitude file.
    amp_dispersion_threshold : float, optional
        The threshold for the amplitude dispersion. Default is 0.25.
    existing_amp_mean_file : Optional[Filename], optional
        An existing amplitude mean file to use, by default None.
    existing_amp_dispersion_file : Optional[Filename], optional
        An existing amplitude dispersion file to use, by default None.
    nodata_mask : Optional[np.ndarray]
        If provided, skips computing PS over areas where the mask is False
        Otherwise, loads input data from everywhere and calculates.
    update_existing : bool, optional
        If providing existing amp mean/dispersion files, combine them with the
        data from the current SLC stack.
        If False, simply uses the existing files to create as PS mask.
        Default is False.
    block_shape : tuple[int, int], optional
        The 2D block size to load all bands at a time.
        Default is (512, 512)
    show_progress : bool, default=True
        If true, displays a `rich.ProgressBar`.
    """
    if existing_amp_dispersion_file and existing_amp_mean_file and not update_existing:
        logger.info("Using existing amplitude dispersion file, skipping calculation.")
        # Just use what's there, copy to the expected output locations
        _use_existing_files(
            existing_amp_mean_file=existing_amp_mean_file,
            existing_amp_dispersion_file=existing_amp_dispersion_file,
            output_file=output_file,
            output_amp_mean_file=output_amp_mean_file,
            output_amp_dispersion_file=output_amp_dispersion_file,
            amp_dispersion_threshold=amp_dispersion_threshold,
        )
        return

    # Otherwise, we need to calculate the PS files from the SLC stack
    # Initialize the output files with zeros
    file_list = [output_file, output_amp_dispersion_file, output_amp_mean_file]
    for fn, dtype, nodata in zip(file_list, FILE_DTYPES, NODATA_VALUES):
        io.write_arr(
            arr=None,
            like_filename=slc_vrt_file,
            output_name=fn,
            nbands=1,
            dtype=dtype,
            nodata=nodata,
        )

    vrt_stack = VRTStack.from_vrt_file(slc_vrt_file)

    # Initialize the intermediate arrays for the calculation
    magnitude = np.zeros((len(vrt_stack), *block_shape), dtype=np.float32)

    skip_empty = nodata_mask is None

    writer = io.Writer()
    # Make the generator for the blocks
    block_gen = vrt_stack.iter_blocks(
        block_shape=block_shape,
        skip_empty=skip_empty,
        nodata_mask=nodata_mask,
        show_progress=show_progress,
    )
    for cur_data, (rows, cols) in block_gen:
        cur_rows, cur_cols = cur_data.shape[-2:]

        if not (np.all(cur_data == 0) or np.all(np.isnan(cur_data))):
            magnitude_cur = np.abs(cur_data, out=magnitude[:, :cur_rows, :cur_cols])
            mean, amp_disp, ps = calc_ps_block(
                # use min_count == size of stack so that ALL need to be not Nan
                magnitude_cur,
                amp_dispersion_threshold,
                min_count=len(magnitude_cur),
            )

            # Use the UInt8 type for the PS to save.
            # For invalid pixels, set to max Byte value
            ps = ps.astype(FILE_DTYPES.ps)
            ps[amp_disp == 0] = NODATA_VALUES.ps
        else:
            # Fill the block with nodata
            ps = np.ones((cur_rows, cur_cols), dtype=FILE_DTYPES.ps) * NODATA_VALUES.ps
            mean = np.full(
                (cur_rows, cur_cols), NODATA_VALUES.amp_mean, dtype=FILE_DTYPES.amp_mean
            )
            amp_disp = np.full(
                (cur_rows, cur_cols),
                NODATA_VALUES.amp_dispersion,
                dtype=FILE_DTYPES.amp_dispersion,
            )

        # Write amp dispersion and the mean blocks
        writer.queue_write(mean, output_amp_mean_file, rows.start, cols.start)
        writer.queue_write(amp_disp, output_amp_dispersion_file, rows.start, cols.start)
        writer.queue_write(ps, output_file, rows.start, cols.start)

    logger.info(f"Waiting to write {writer.num_queued} blocks of data.")
    writer.notify_finished()
    logger.info("Finished writing out PS files")


def calc_ps_block(
    stack_mag: ArrayLike,
    amp_dispersion_threshold: float = 0.25,
    min_count: Optional[int] = None,
):
    r"""Calculate the amplitude dispersion for a block of data.

    The amplitude dispersion is defined as the standard deviation of a pixel's
    magnitude divided by the mean of the magnitude:

    \[
    d_a = \frac{\sigma(|Z|)}{\mu(|Z|)}
    \]

    where $Z \in \mathbb{R}^{N}$ is one pixel's complex data for $N$ SLCs.

    Parameters
    ----------
    stack_mag : ArrayLike
        The magnitude of the stack of SLCs.
    amp_dispersion_threshold : float, optional
        The threshold for the amplitude dispersion to label a pixel as a PS:
            ps = amp_disp < amp_dispersion_threshold
        Default is 0.25.
    min_count : int, optional
        The minimum number of valid pixels to calculate the mean and standard
        deviation. If the number of valid pixels is less than `min_count`,
        then the mean and standard deviation are set to 0 (and the pixel is
        not a PS). Default is 90% the number of SLCs: `int(0.9 * stack_mag.shape[0])`.

    Returns
    -------
    mean : np.ndarray
        The mean amplitude for the block.
        dtype: float32
    amp_disp : np.ndarray
        The amplitude dispersion for the block.
        dtype: float32
    ps : np.ndarray
        The persistent scatterers for the block.
        dtype: bool

    Notes
    -----
    The min_count is used to prevent the mean and standard deviation from being
    calculated for pixels that are not valid for most of the SLCs. This happens
    when the burst footprints shift around and pixels near the edge get only one or
    two acquisitions.
    Since fewer samples are used to calculate the mean and standard deviation,
    there is a higher false positive risk for these edge pixels.
    """
    if np.iscomplexobj(stack_mag):
        raise ValueError("The input `stack_mag` must be real-valued.")

    if min_count is None:
        min_count = int(0.9 * stack_mag.shape[0])

    with warnings.catch_warnings():
        # ignore the warning about nansum/nanmean of empty slice
        warnings.simplefilter("ignore", category=RuntimeWarning)

        mean = np.nanmean(stack_mag, axis=0)
        std_dev = np.nanstd(stack_mag, axis=0)
        count = np.count_nonzero(~np.isnan(stack_mag), axis=0)
        amp_disp = std_dev / mean
    # Mask out the pixels with too few valid pixels
    amp_disp[count < min_count] = np.nan
    # replace nans/infinities with 0s, which will mean nodata
    mean = np.nan_to_num(mean, nan=0, posinf=0, neginf=0, copy=False)
    amp_disp = np.nan_to_num(amp_disp, nan=0, posinf=0, neginf=0, copy=False)

    ps = amp_disp < amp_dispersion_threshold
    ps[amp_disp == 0] = False
    return mean, amp_disp, ps


def _use_existing_files(
    *,
    existing_amp_mean_file: Filename,
    existing_amp_dispersion_file: Filename,
    output_file: Filename,
    output_amp_mean_file: Filename,
    output_amp_dispersion_file: Filename,
    amp_dispersion_threshold: float,
) -> None:
    amp_disp = io.load_gdal(existing_amp_dispersion_file, masked=True)
    ps = amp_disp < amp_dispersion_threshold
    ps = ps.astype(np.uint8)
    # Set the PS nodata value to the max uint8 value
    ps[(amp_disp == 0) | amp_disp.mask] = NODATA_VALUES.ps
    io.write_arr(
        arr=ps,
        like_filename=existing_amp_dispersion_file,
        output_name=output_file,
        nodata=NODATA_VALUES.ps,
    )
    # Copy the existing amp mean file/amp dispersion file
    shutil.copy(existing_amp_dispersion_file, output_amp_dispersion_file)
    shutil.copy(existing_amp_mean_file, output_amp_mean_file)


def multilook_ps_mask(
    strides: dict[str, int],
    ps_mask_file: Filename,
    output_file: Optional[Filename] = None,
) -> Path:
    """Create a multilooked version of the full-res PS mask.

    Parameters
    ----------
    strides : dict[str, int]
        Decimation factor for 'x', 'y'
    ps_mask_file : Filename
        Name of input full-res uint8 PS mask file
    output_file : Optional[Filename], optional
        Name of file to save result to.
        Defaults to same as `ps_mask_file`, but with "_looked" added before suffix.

    Returns
    -------
    output_file : Path
    """
    if strides == {"x": 1, "y": 1}:
        logger.info("No striding request, skipping multilook.")
        return Path(ps_mask_file)
    if output_file is None:
        ps_suffix = Path(ps_mask_file).suffix
        out_path = Path(str(ps_mask_file).replace(ps_suffix, f"_looked{ps_suffix}"))
        logger.info(f"Saving a looked PS mask to {out_path}")
    else:
        out_path = Path(output_file)

    if Path(out_path).exists():
        logger.info(f"{out_path} exists, skipping.")
        return out_path

    ps_mask = io.load_gdal(ps_mask_file, masked=True).astype(bool).filled(False)
    full_rows, full_cols = ps_mask.shape
    ps_mask_looked = utils.take_looks(
        ps_mask, strides["y"], strides["x"], func_type="any", edge_strategy="pad"
    )
    # make sure it's the same size as the MLE result/temp_coh after padding
    out_rows, out_cols = full_rows // strides["y"], full_cols // strides["x"]
    ps_mask_looked = ps_mask_looked[:out_rows, :out_cols]
    ps_mask_looked = ps_mask_looked.astype("uint8")
    io.write_arr(
        arr=ps_mask_looked,
        like_filename=ps_mask_file,
        output_name=out_path,
        strides=strides,
        nodata=255,
    )
    return out_path
