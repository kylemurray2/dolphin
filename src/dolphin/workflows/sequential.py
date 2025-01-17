"""Estimate wrapped phase using batches of ministacks.

References
----------
    [1] Ansari, H., De Zan, F., & Bamler, R. (2017). Sequential estimator: Toward
    efficient InSAR time series analysis. IEEE Transactions on Geoscience and
    Remote Sensing, 55(10), 5637-5652.
"""
from __future__ import annotations

from collections import defaultdict
from itertools import chain
from os import fspath
from pathlib import Path
from typing import Optional

from osgeo_utils import gdal_calc

from dolphin import io
from dolphin._log import get_log
from dolphin._types import Filename
from dolphin.stack import VRTStack

from ._enums import ShpMethod
from .single import run_wrapped_phase_single

logger = get_log(__name__)

__all__ = ["run_wrapped_phase_sequential"]


def run_wrapped_phase_sequential(
    *,
    slc_vrt_file: Filename,
    output_folder: Filename,
    half_window: dict,
    strides: dict = {"x": 1, "y": 1},
    ministack_size: int = 10,
    mask_file: Optional[Filename] = None,
    ps_mask_file: Optional[Filename] = None,
    amp_mean_file: Optional[Filename] = None,
    amp_dispersion_file: Optional[Filename] = None,
    shp_method: ShpMethod = ShpMethod.NONE,
    shp_alpha: float = 0.05,
    shp_nslc: Optional[int],
    beta: float = 0.01,
    block_shape: tuple[int, int] = (512, 512),
    n_workers: int = 1,
    gpu_enabled: bool = True,
) -> tuple[list[Path], list[Path], Path]:
    """Estimate wrapped phase using batches of ministacks."""
    output_folder = Path(output_folder)
    v_all = VRTStack.from_vrt_file(slc_vrt_file)
    file_list_all = v_all.file_list
    date_list_all = v_all.dates

    if shp_nslc is None:
        shp_nslc = len(file_list_all)

    logger.info(f"{v_all}: from {v_all.file_list[0]} to {v_all.file_list[-1]}")

    # Map of {ministack_index: [output_slc_files]}
    output_slc_files: dict[int, list] = defaultdict(list)
    comp_slc_files: list[Path] = []
    tcorr_files: list[Path] = []

    # Solve each ministack using the current chunk (and the previous compressed SLCs)
    ministack_starts = range(0, len(file_list_all), ministack_size)
    for mini_idx, full_stack_idx in enumerate(ministack_starts):
        cur_slice = slice(full_stack_idx, full_stack_idx + ministack_size)
        cur_files = file_list_all[cur_slice].copy()
        cur_dates = date_list_all[cur_slice].copy()

        # Make the current ministack output folder using the start/end dates
        d0 = cur_dates[0][0]
        d1 = cur_dates[-1][0]
        start_end = io._format_date_pair(d0, d1)
        cur_output_folder = output_folder / start_end
        cur_output_folder.mkdir(parents=True, exist_ok=True)

        msg = f"Processing {len(cur_files)} SLCs."
        msg += f"Output folder: {cur_output_folder}"
        logger.info(msg)
        # Add the existing compressed SLC files to the start
        cur_files = comp_slc_files + cur_files
        # TODO TESTING:
        # LIMIT THE NUM COMP SLCS TO 3!
        # cur_files = comp_slc_files[-3:] + cur_files
        cur_vrt = VRTStack(
            cur_files,
            outfile=cur_output_folder / f"{start_end}.vrt",
            sort_files=False,
            subdataset=v_all.subdataset,
        )
        # TODO: what do we gain by choosing a different ref_idx here...
        ref_idx = 0
        cur_output_files, cur_comp_slc_file, tcorr_file = run_wrapped_phase_single(
            slc_vrt_file=cur_vrt,
            output_folder=cur_output_folder,
            half_window=half_window,
            strides=strides,
            # TODO: what situations do we need to set reference-idx != 0
            reference_idx=ref_idx,
            beta=beta,
            mask_file=mask_file,
            ps_mask_file=ps_mask_file,
            amp_mean_file=amp_mean_file,
            amp_dispersion_file=amp_dispersion_file,
            shp_method=shp_method,
            shp_alpha=shp_alpha,
            shp_nslc=shp_nslc,
            block_shape=block_shape,
            n_workers=n_workers,
            gpu_enabled=gpu_enabled,
        )

        output_slc_files[mini_idx] = cur_output_files
        comp_slc_files.append(cur_comp_slc_file)
        tcorr_files.append(tcorr_file)

    ##############################################

    # Average the temporal coherence files in each ministack
    # TODO: do we want to include the date span in this filename?
    output_tcorr_file = output_folder / "tcorr_average.tif"
    # we can pass the list of files to gdal_calc, which interprets it
    # as a multi-band file
    if len(tcorr_files) > 1:
        logger.info(f"Averaging temporal coherence files into: {output_tcorr_file}")
        gdal_calc.Calc(
            NoDataValue=0,
            format="GTiff",
            outfile=fspath(output_tcorr_file),
            type="Float32",
            quiet=True,
            overwrite=True,
            creation_options=io.DEFAULT_TIFF_OPTIONS,
            A=tcorr_files,
            calc="numpy.nanmean(A, axis=0)",
        )
    else:
        tcorr_files[0].rename(output_tcorr_file)

    # Combine the separate SLC output lists into a single list
    all_slc_files = list(chain.from_iterable(output_slc_files.values()))

    pl_outputs = []
    for slc_fname in all_slc_files:
        slc_fname.rename(output_folder / slc_fname.name)
        pl_outputs.append(output_folder / slc_fname.name)

    comp_outputs = []
    for p in comp_slc_files:
        p.rename(output_folder / p.name)
        comp_outputs.append(output_folder / p.name)

    return pl_outputs, comp_outputs, output_tcorr_file
