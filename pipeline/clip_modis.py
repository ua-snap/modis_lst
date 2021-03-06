"""Clip the processed MODIS files to the 1km WRF grid"""

import argparse, datetime, glob, os, subprocess, time
import numpy as np
import pandas as pd
import rasterio as rio
import xarray as xr
from helpers import check_env, add_metadata
from multiprocessing import Pool


def make_cutline(template_fp, temp_dir):
    """Make shapefile outline of WRF grid"""
    shp_fp = os.path.join(temp_dir, "clip_modis.shp")
    if os.path.exists(shp_fp):
        os.unlink(shp_fp)
    _ = subprocess.call(["gdaltindex", shp_fp, template_fp])
    return shp_fp


def clip_modis(shp_fp, fp, out_fp):
    """Use gdalwarp to clip MODIS file to the WRF template"""
    _ = subprocess.call(
        [
            "gdalwarp",
            "-cutline",
            shp_fp,
            "-crop_to_cutline",
            "-q",
            "-overwrite",
            fp,
            out_fp,
        ]
    )
    with rio.open(out_fp, mode="r+") as out:
        arr = out.read(1)
        arr[arr == 0] = -9999
        out.write(arr, 1)
    return out_fp


def wrap_clip(args):
    """Wrapper for clipping in parallel"""
    return clip_modis(*args)


def read_band(fn, band=1):
    """Read a GeoTIFF band's data, for clipped MODIS"""
    with rio.open(fn) as rst:
        return rst.read(band)


def get_dates(fps):
    """Get datetime array of dates from filenames, for NetCDFs"""
    jd_lst = [os.path.basename(fp).split("_")[-2] for fp in fps]
    dt = [datetime.datetime.strptime(jd_str, "%Y%j") for jd_str in jd_lst]
    dt_arr = np.array(dt, dtype="datetime64")
    return dt_arr


if __name__ == "__main__":
    # check environment
    _ = check_env()
    parser = argparse.ArgumentParser(
        description="Clip processed MODIS data to the 1km WRF grid"
    )
    parser.add_argument(
        "-n",
        "--ncpus",
        action="store",
        dest="ncpus",
        type=int,
        help="Number of cores to use with multiprocessing",
    )
    args = parser.parse_args()
    ncpus = args.ncpus
    # hard-coded variables for now
    wrf_var = "tsk"
    mod_var = "lst"
    scratch_dir = os.getenv("SCRATCH_DIR")
    temp_dir = os.path.join(scratch_dir, "temp")
    out_base_dir = os.getenv("OUTPUT_DIR")
    modis_dir = os.path.join(scratch_dir, "MODIS", "processed")
    # output directory for bands
    out_bands_dir_tp = os.path.join(scratch_dir, "MODIS", "clipped", mod_var, "{}")
    # output directory for datasets
    out_dir = os.path.join(out_base_dir, "aligned-WRF-MODIS", "MODIS")
    if not os.path.exists(out_dir):
        _ = os.makedirs(out_dir)
    
    # make cutline fp
    template_fp = os.path.join(scratch_dir, "ancillary", "wrf_3338_template.tif")
    shp_fp = make_cutline(template_fp, temp_dir)
    
    # stuff for metadata
    src_str = "{}.006 land surface temperature, tiles 11, 12"
    title = "1km WRF-aligned MODIS LST"
    long_name = "Land surface temperature"
    
    # clip data for each sensor
    sensors = ["MOD11A2", "MYD11A2"]
    for sensor in sensors:
        # 1) clip individual GeoTIFFs
        modis_fps = sorted(glob.glob(os.path.join(modis_dir, f"{sensor}*")))
        out_bands_dir = out_bands_dir_tp.format(sensor)
        if not os.path.exists(out_bands_dir):
            _ = os.makedirs(out_bands_dir)
        print("Clipping MODIS GeoTIFFs to WRF", end="...")
        tic = time.perf_counter()
        
        # set up args for clipping in parallel
        clip_args = []
        for fp in modis_fps:
            fn = os.path.basename(fp).split("_")
            jdate = fn[-5][1:]
            # only need dates in Mar-Oct
            if (int(jdate[-3:]) >= 89) & (int(jdate[-3:]) <= 305):
                fn = f"{mod_var}_{sensor}_InteriorAK_{jdate}_clipped.tif"
                out_fp = os.path.join(out_bands_dir, fn)
                clip_args.append((shp_fp, fp, out_fp))
                
        # clip in parallel
        pool = Pool(ncpus)
        bands_fps = pool.map(wrap_clip, clip_args)
        pool.close()
        pool.join()
        
        duration = round(time.perf_counter() - tic, 1)
        print(f"done, duration: {duration}s")
        
        # 2) assemble into NetCDFs
        print(f"Creating NetCDF dataset for {sensor}", end="...")
        tic = time.perf_counter()
        
        # read clipped bands in parallel
        pool = Pool(ncpus)
        out_arrs = pool.map(read_band, bands_fps)
        pool.close()
        pool.join()
        bands_arr = np.array(out_arrs)
        
        with rio.open(bands_fps[0]) as tmp:
            new_meta = tmp.meta.copy()
            new_meta.update(count=bands_arr.shape[0])
            # for netcdf
            idx = np.arange(tmp.width)
            idy = np.arange(tmp.height)
            xc = tmp.xy(np.repeat(0, idx.shape), idx)[0]
            yc = tmp.xy(idy, np.repeat(0, idy.shape))[1]
            
        # write to netcdf
        dates = get_dates(bands_fps)
        # Fill missing data with NaN (default _FillValue)
        bands_arr[bands_arr == -9999] = np.nan
        ds = xr.Dataset(
            {mod_var: (["date", "yc", "xc"], bands_arr)},
            coords={"xc": xc, "yc": yc, "date": dates,},
        )
        ds = add_metadata(ds, mod_var, long_name, title, src_str.format(sensor))
        out_nc_fp = os.path.join(out_dir, f"lst_{sensor}_aligned.nc")
        ds.to_netcdf(out_nc_fp)
        duration = round(time.perf_counter() - tic, 1)
        print(f"done, duration: {duration}s")
        print(f"Data saved to {out_nc_fp}")
