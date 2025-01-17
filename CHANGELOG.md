# Unreleased


# [0.4.1](https://github.com/isce-framework/dolphin/compare/v0.4.0...v0.4.1) - 2023-09-08

**Dependencies**
- Added back isce3

# [0.4.0](https://github.com/isce-framework/dolphin/compare/v0.3.0...v0.4.0) - 2023-09-07


**Changed**

- Split apart OPERA-specific needs from more general library/workflow functionality
- Removed the final NetCDF product creation
  - Many rasters in the `scratch/` folder are of general interest after running the workflow
  - Changed folder structure so that there's not longer a top-level `scratch/` and `output/` by default
- Changed the required dependencies so the `isce3` unwrapper is optional, as people may wish to implement their own custom parallel unwrapping

**Dependencies**

Dropped:
- h5netcdf
- pillow

Now optional:
- isce3 (for unwrapping)

# [0.3.0](https://github.com/isce-framework/dolphin/compare/v0.2.0...v0.3.0) - 2023-08-23

**Added**

- Save a multilooked version of the PS mask for output inspection

**Changed**

- Pydantic models were upgraded to V2
- Refactored the blockwise IO into `_blocks.py`.
  - The iteration now happens over the output grid for easier dilating/padding when using `strides`
  - New classes with `BlockIndices` and `BlockManager` for easier mangement of the different slices

**Dependencies**

- pydantic >= 2.1

# [0.2.0](https://github.com/isce-framework/dolphin/compare/v0.1.0...v0.2.0) - 2023-07-25

**Added**

- For OPERA CSLC inputs, we now read the nodata polygon and skip loading regions of the SLC stack which are all nodata.
  - This led to a reduction of 30-50% in wrapped phase estimation runtime for each burst stack.
- Sample test data for the `dolphin` package loaded onto Zenodo.
- Adds 3 methods of computing a variable statistically homogeneous pixel (SHP) window when estimating the covariance matrix:
  - Kolmogorov-Smirnov test (KS-test)
  - Generalized likelihood ratio test (GLRT)
  - Kullback-Leibler divergence/distance test (KLD)
  - `"rect"` is also an option for skipping any statistical test and using the full rectangular multilook window
- Also included a script to view the window in an interactive matplotlib figure (matplotlib must be installed separately)
- Added a simple method to check for adjacent-pixel unwrapping errors in `unwrap.compute_phase_diffs`
- Adds a method `utils.get_cpu_count` which returns either `os.cpu_count`, or (if running in a Docker container) the number of CPUs allocated by Docker
- If processing stacks from separate bursts, added option `n_parallel_bursts` to `Workflow` to run in parallel processes.
- Created a script to test the incremental/near-real-time version of phase linking
- Added a new CLI command `dolphin unwrap` to unwrap a single interferogram/a directory of interferograms in parallel.
- Added ability to specify a glob pattern for input CSLC files in the YAML config
- Saves a multilooked PS mask for the default workflow output
- Allows the user to specify a desired bounds for the final stitched result

**Changes**

- Default OPERA dataset is now within `/data`, reflecting the new COMPASS product spec since CalVal
- Passing an existing file to `VRTStack` will no longer error unless `fail_on_overwrite=True`. The default just prints out the overwrite is happening. This prevents multiple runs in the same folder from errorings just for creating a reference to the SLC files.
- The environment variable `NUMBA_NUM_THREADS` is set using the passed in config to prevent numba from using all CPUs during `prange` calls
- The `sequential.py` module uses a different implementation of the sequential estimator to avoid the need for a datum adjustment.
- The scratch directory holding unwrapped interferograms is named `unwrapped` instead of `unwrap`
- Stitching files now can accept downsampled versions and product the correct geo metadata

**Fixed**

- Calculating the nodata mask using the correct input geotransform
- Trims the overlapped region of the phase linking step when iterating in blocks

**Dependencies**

- shapely >= 1.8
- Numba now supports Python 3.11, so we can drop the Python<3.11 version restriction.

Added testing requirements:
- pooch
- pillow>=7.0


# [0.1.0](https://github.com/isce-framework/dolphin/compare/v0.0.4...v0.1.0) - 2023-03-31

- First version of the `_product.py` module to output the combined NetCDF product file.
- `_pge_runconfig.py` module to handle the separate PGE-compatible configuration, which translates to-from the `Workflow` object.
- `docker/build-docker-image.sh` script to build the docker image.
- Release scripts for generating documentation, script for validating output data by @gmgunter .
- Use of a spatial correlation estimate for unwrapping purposes, rather than temporal coherence.
  - This is much more useful when the stack size is small (high temporal coherence), and `snaphu` is used for unwrapping.
- `masking.py` module for masking the interferogram/combined multiple external masks of varying 1/0 conventions.
- Ability to use existing amplitude mean/dispersion files for the PS portion of the workflow, skipping the step where we compute it using the SLC stack. Useful for small stack sizes
- Added a `create_only` option to `write_arr` to create an empty file without writing data (e.g. to check the boundary results of stitching)


**Changed**
- The YAML output/input functions are moved to a `YamlModel` class, which is a subclass of `pydantic.BaseModel`.
  - This allows us to use it in both `config.py` and `_pge_runconfig.py`.
- Refactoring of the `Workflow` layout to more easily extract the input/output files for the PGE run.

**Fixed**

- Compressed SLC outputs were getting corrupted upon writing when using strides > 1.
- Single-update interferograms where the first SLC input is a compressed SLC was broken (using the wrong size raster).
  - Now the result will simply copy over the phase-linking result, which is already referenced to the first raster.

**Dependencies**

Added requirements:

- h5netcdf>=1.1
- Avoid HDF5 version 1.12.1 until NetCDF loading issue is fixed

# [0.0.4](https://github.com/isce-framework/dolphin/compare/v0.0.3...v0.0.4) - 2023-03-17

**Added**

- Created first version of the single-update workflow, usable with `dolphin config --single`
- `_background.py` module as the abstract classes for background input/output with `EagerLoader` and `Writer`.
- `from_vrt_file` for the `VRTInterferogram` class.
- Arbitrary interferogram index selection in `Network` class.
- Parallel CPU eigenvector finding using `scipy.linalg.eigh`.
- PS selection for strided outputs using the average PS phase within a window (that contains multiple PS).
- Comments in the YAML file output by the `dolphin config` command.


**Changed**

- The main workflow has been renamed to `s1_disp.py` to better reflect the workflow, since it can handle both single and stack workflows.
    - The `sequential.py` and `single.py` are where these differences are handled.
- More uniform naming in `io.get_raster_<X>` functions.
- The SLC compression is now done in `_compress.py` to declutter the `mle.py` module.
- Replace `tqdm` with `rich` for progress bars.
- The `unwrap.py` module now uses isce3 to unwrap the interferogram.

- Docs are now using the mkdocs `material` theme.

**Removed**

- `utils.parse_slc_strings` in favor of always using `utils.get_dates`.
- `io.get_stack_nodata_mask`. This will be done using the nodata polygon, or not at all.


**Dependencies**

Added requirements:

- rich>=12.0
- threadpoolctl>=3.0
- isce3>=0.8.0
- pyproj>=3.3
- Dropped support for Python 3.7

For docs:
- mkdocs-material
- pymdown-extensions

Removed requirements:

- tqdm


# [0.0.3](https://github.com/isce-framework/dolphin/compare/v0.0.2...v0.0.3) - 2023-01-26

**Added**

- Ability for `VRTStack` to handle HDF5 files with subdatasets.
    - The OPERA specific HDF5 files are now supported without extra configuration.
- First stitching of interferograms in `stitching.py`.
    - Users can pass multiple SLC burst (like COMPASS bursts) per date, and the workflow will process per stack then stitch per date.
- More features for `load_gdal` to load in blocks.

**Changed**

- A small amount of regularization on the coherence matrix is done before inversion during phase linking to avoid singular matrices.
- Renamed module to `_log.py`
- `workflows/wrapped_phase.py` absorbed much logic formerly in `s1_disp_stack.py`.

# [0.0.2](https://github.com/isce-framework/dolphin/compare/v0.0.1...v0.0.2) - 2023-01-24

**Added**

- Created first version of the `s1_disp_stack.py` workflow.
- Created the modules necessary for first version of the sequential workflow, including
    - `ps.py`
    - `sequential.py`
    - `io.py`
    - `interferogram.py`
    - `utils.py`
    - `unwrap.py`
    - `stitching.py`
    - `vrt.py`
- Created the `phase_link` subpackage for wrapped phase estimation.


Added requirements:

- pyproj>=3.2
- tqdm>=4.60


# [0.0.1] - 2022-12-09

**Added**

- Created the `config` module to handle the configuration of the workflows
- Command line interface for running the workflows
- Outline of project structure and utilities


**Dependencies**

Added requirements:

- gdal>=3.3
- h5py>=3.6
- numba>=0.54
- numpy>=1.20
- pydantic>=1.10
- pymp-pypi>=0.4.5
- ruamel_yaml>=0.15
- scipy>=1.5

Currently, Python 3.7 is supported, but 3.11 is not due numba not yet supporting Python 3.11.
