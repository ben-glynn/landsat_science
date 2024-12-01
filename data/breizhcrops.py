import torch
from torch.utils.data import Dataset
import os
import numpy as np

class BreizhCrops(Dataset):
    def __init__(self, partition="train", root="breizhcrops_dataset", sequencelength=70, year=2017, return_id=False):
        assert partition in ["train", "valid", "eval"]
        if partition == "train":
            frh01 = BzhBreizhCrops("frh01", root=root, transform=lambda x: x, preload_ram=True, year=year)
            frh02 = BzhBreizhCrops("frh02", root=root, transform=lambda x: x, preload_ram=True, year=year)
            self.ds = torch.utils.data.ConcatDataset([frh01, frh02])
        elif partition == "valid":
            self.ds = BzhBreizhCrops("frh03", root=root, transform=lambda x: x, preload_ram=True, year=year)
        elif partition == "eval":
            self.ds = BzhBreizhCrops("frh04", root=root, transform=lambda x: x, preload_ram=True, year=year)

        self.sequencelength = sequencelength
        self.return_id = return_id

    def __len__(self):
        return len(self.ds)

    def __getitem__(self, item):
        X,y,id = self.ds[item]

        # take bands and normalize
        # ['B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B8A', 'B9', 'B10', 'B11', 'B12', '...']
        X = X[:,:13] * 1e-4

        # get length of this sample
        t = X.shape[0]


        if t < self.sequencelength:
            # time series shorter than "sequencelength" will be zero-padded
            npad = self.sequencelength - t
            X = np.pad(X, [(0, npad), (0, 0)], 'constant', constant_values=0)
        elif t > self.sequencelength:
            # time series longer than "sequencelength" will be sub-sampled
            idxs = np.random.choice(t, self.sequencelength, replace=False)
            idxs.sort()
            X = X[idxs]

        X = torch.from_numpy(X).type(torch.FloatTensor)

        X,y  = X, y.repeat(self.sequencelength)
        if self.return_id:
            return X, y, id
        else:
            return X, y


import os

import geopandas as gpd
import h5py
import numpy as np
import pandas as pd
from torch.utils.data import Dataset
from tqdm import tqdm
import torch
import numpy as np


BANDS = {
    "L1C": ['B1', 'B10', 'B11', 'B12', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8',
            'B8A', 'B9', 'QA10', 'QA20', 'QA60', 'doa', 'label', 'id'],
    "L2A": ['doa', 'id', 'code_cultu', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8',
            'B8A', 'B11', 'B12', 'CLD', 'EDG', 'SAT']
}

SELECTED_BANDS = {
    "L1C": ['B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B8A', 'B9', 'B10', 'B11', 'B12',
            'QA10', 'QA20', 'QA60', 'doa'],
    "L2A": ['doa', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B8A', 'B11', 'B12',
            'CLD', 'EDG', 'SAT', ]
}

PADDING_VALUE = -1


class BzhBreizhCrops(Dataset):

    def __init__(self,
                 region,
                 root="breizhcrops_dataset",
                 year=2017, level="L1C",
                 transform=None,
                 target_transform=None,
                 filter_length=0,
                 verbose=False,
                 load_timeseries=True,
                 recompile_h5_from_csv=False,
                 preload_ram=False):
        """
        :param region: dataset region. choose from "frh01", "frh02", "frh03", "frh04", "belle-ile"
        :param root: where the data will be stored. defaults to `./breizhcrops_dataset`
        :param year: year of the data. currently only `2017`
        :param level: Sentinel 2 processing level. Either `L1C` (top of atmosphere) or `L2A` (bottom of atmosphere)
        :param transform: a transformation function applied to the raw data before retrieving a sample. Can be used for featured extraction or data augmentaiton
        :param target_transform: a transformation function applied to the label.
        :param filter_length: time series shorter than `filter_length` will be ignored
        :param bool verbose: verbosity flag
        :param bool load_timeseries: if False, no time series data will be loaded. Only index file and class initialization. Used mostly for tests
        :param bool recompile_h5_from_csv: downloads raw csv files and recompiles the h5 databases. Only required when dealing with new datasets
        :param bool preload_ram: loads all time series data in RAM at initialization. Can speed up training if data is stored on HDD.
        """
        assert year in [2017, 2018]
        assert level in ["L1C", "L2A"]
        assert region in ["frh01", "frh02", "frh03", "frh04", "belle-ile"]

        if transform is None:
            transform = get_default_transform(level)
        if target_transform is None:
            target_transform = get_default_target_transform()
        self.transform = transform
        self.target_transform = target_transform

        self.region = region.lower()
        self.bands = BANDS[level]

        self.verbose = verbose
        self.year = year
        self.level = level

        if verbose:
            print(f"Initializing BreizhCrops region {region}, year {year}, level {level}")

        self.root = root
        self.h5path, self.indexfile, self.codesfile, self.shapefile, self.classmapping, self.csvfolder = \
            self.build_folder_structure(self.root, self.year, self.level, self.region)

        self.load_classmapping(self.classmapping)

        if os.path.exists(self.h5path):
            print(os.path.getsize(self.h5path), FILESIZES[year][level][region])
            h5_database_ok = os.path.getsize(self.h5path) == FILESIZES[year][level][region]
        else:
            h5_database_ok = False

        if not os.path.exists(self.indexfile):
            download_file(INDEX_FILE_URLs[year][level][region], self.indexfile)

        if not h5_database_ok and recompile_h5_from_csv and load_timeseries:
            self.download_csv_files()
            self.write_index()
            self.write_h5_database_from_csv(self.index)
        if not h5_database_ok and not recompile_h5_from_csv and load_timeseries:
            self.download_h5_database()

        self.index = pd.read_csv(self.indexfile, index_col=None)
        self.index = self.index.loc[self.index["CODE_CULTU"].isin(self.mapping.index)]
        if verbose:
            print(f"kept {len(self.index)} time series references from applying class mapping")

        # filter zero-length time series
        if self.index.index.name != "idx":
            self.index = self.index.loc[self.index.sequencelength > filter_length].set_index("idx")

        self.maxseqlength = int(self.index["sequencelength"].max())

        if not os.path.exists(self.codesfile):
            download_file(CODESURL, self.codesfile)
        self.codes = pd.read_csv(self.codesfile, delimiter=";", index_col=0)

        if preload_ram:
            self.X_list = list()
            with h5py.File(self.h5path, "r") as dataset:
                for idx, row in tqdm(self.index.iterrows(), desc="loading data into RAM", total=len(self.index)):
                    self.X_list.append(np.array(dataset[(row.path)]))
        else:
            self.X_list = None

        self.index.rename(columns={"meanQA60": "meanCLD"}, inplace=True)

        if "classid" not in self.index.columns or "classname" not in self.index.columns or "region" not in self.index.columns:
            # drop fields that are not in the class mapping
            self.index = self.index.loc[self.index["CODE_CULTU"].isin(self.mapping.index)]
            self.index[["classid", "classname"]] = self.index["CODE_CULTU"].apply(lambda code: self.mapping.loc[code])
            self.index["region"] = self.region
            self.index.to_csv(self.indexfile)

        self.get_codes()

    def download_csv_files(self):
        zipped_file = os.path.join(self.root, str(self.year), self.level, f"{self.region}.zip")
        download_file(RAW_CSV_URL[self.year][self.level][self.region], zipped_file)
        unzip(zipped_file, self.csvfolder)
        os.remove(zipped_file)

    def build_folder_structure(self, root, year, level, region):
        """
        folder structure

        <root>
           codes.csv
           classmapping.csv
           <year>
              <region>.shp
              <level>
                 <region>.csv
                 <region>.h5
                 <region>
                     <csv>
                         123123.csv
                         123125.csv
                         ...
        """
        year = str(year)

        os.makedirs(os.path.join(root, year, level, region), exist_ok=True)

        h5path = os.path.join(root, year, level, f"{region}.h5")
        indexfile = os.path.join(root, year, level, region + ".csv")
        codesfile = os.path.join(root, "codes.csv")
        shapefile = os.path.join(root, year, f"{region}.shp")
        classmapping = os.path.join(root, "classmapping.csv")
        csvfolder = os.path.join(root, year, level, region, "csv")

        return h5path, indexfile, codesfile, shapefile, classmapping, csvfolder

    def get_fid(self, idx):
        return self.index[self.index["idx"] == idx].index[0]

    def download_h5_database(self):
        print(f"downloading {self.h5path}.tar.gz")
        download_file(H5_URLs[self.year][self.level][self.region], self.h5path + ".tar.gz", overwrite=True)
        print(f"extracting {self.h5path}.tar.gz to {self.h5path}")
        untar(self.h5path + ".tar.gz")
        print(f"removing {self.h5path}.tar.gz")
        os.remove(self.h5path + ".tar.gz")
        print(f"checking integrity by file size...")
        assert os.path.getsize(self.h5path) == FILESIZES[self.year][self.level][self.region]
        print("ok!")

    def write_h5_database_from_csv(self, index):
        with h5py.File(self.h5path, "w") as dataset:
            for idx, row in tqdm(index.iterrows(), total=len(index), desc=f"writing {self.h5path}"):
                X = self.load(os.path.join(self.root, row.path))
                dataset.create_dataset(row.path, data=X)

    def get_codes(self):
        return self.codes

    def download_geodataframe(self):
        targzfile = os.path.join(os.path.dirname(self.shapefile), self.region + ".tar.gz")
        download_file(SHP_URLs[self.year][self.region], targzfile)
        untar(targzfile)
        os.remove(targzfile)

    def geodataframe(self):

        if not os.path.exists(self.shapefile):
            self.download_geodataframe()

        geodataframe = gpd.GeoDataFrame(self.index.set_index("id"))

        gdf = gpd.read_file(self.shapefile)

        # 2018 shapefile calls ID ID_PARCEL: rename if necessary
        gdf = gdf.rename(columns={"ID_PARCEL": "ID"})

        # copy geometry from shapefile to index file
        geom = gdf.set_index("ID")
        geom.index.name = "id"
        geodataframe["geometry"] = geom["geometry"]
        geodataframe.crs = geom.crs

        return geodataframe.reset_index()

    def load_classmapping(self, classmapping):
        if not os.path.exists(classmapping):
            if self.verbose:
                print(f"no classmapping found at {classmapping}, downloading from {CLASSMAPPINGURL}")
            download_file(CLASSMAPPINGURL, classmapping)
        else:
            if self.verbose:
                print(f"found classmapping at {classmapping}")

        self.mapping = pd.read_csv(classmapping, index_col=0).sort_values(by="id")
        self.mapping = self.mapping.set_index("code")
        self.classes = self.mapping["id"].unique()
        self.classname = self.mapping.groupby("id").first().classname.values
        self.klassenname = self.classname
        self.nclasses = len(self.classes)
        if self.verbose:
            print(f"read {self.nclasses} classes from {classmapping}")

    def load_raw(self, csv_file):
        """['B1', 'B10', 'B11', 'B12', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8',
               'B8A', 'B9', 'QA10', 'QA20', 'QA60', 'doa', 'label', 'id']"""
        sample = pd.read_csv(os.path.join(self.csvfolder, os.path.basename(csv_file)), index_col=0).dropna()

        # convert datetime to int
        sample["doa"] = pd.to_datetime(sample["doa"]).astype(int)
        sample = sample.groupby(by="doa").first().reset_index()

        return sample

    def load(self, csv_file):
        sample = self.load_raw(csv_file)

        selected_bands = SELECTED_BANDS[self.level]
        X = np.array(sample[selected_bands].values)
        if np.isnan(X).any():
            t_without_nans = np.isnan(X).sum(1) > 0
            X = X[~t_without_nans]

        return X

    def load_culturecode_and_id(self, csv_file):
        sample = self.load_raw(csv_file)
        X = np.array(sample.values)

        if self.level == "L1C":
            cc_index = self.bands.index("label")
        else:
            cc_index = self.bands.index("code_cultu")
        id_index = self.bands.index("id")

        if len(X) > 0:
            field_id = X[0, id_index]
            culture_code = X[0, cc_index]
            return culture_code, field_id

        else:
            return None, None

    def __len__(self):
        return len(self.index)

    def __getitem__(self, index):
        row = self.index.iloc[index]

        if self.X_list is None:
            with h5py.File(self.h5path, "r") as dataset:
                X = np.array(dataset[(row.path)])
        else:
            X = self.X_list[index]

        # translate CODE_CULTU to class id
        y = self.mapping.loc[row["CODE_CULTU"]].id

        # npad = self.maxseqlength - X.shape[0]
        # X = np.pad(X, [(0, npad), (0, 0)], 'constant', constant_values=PADDING_VALUE)

        if self.transform is not None:
            X = self.transform(X)
        if self.target_transform is not None:
            y = self.target_transform(y)

        return X, y, row.id

    def write_index(self):
        csv_files = os.listdir(self.csvfolder)
        listcsv_statistics = list()
        i = 1

        for csv_file in tqdm(csv_files):
            if self.level == "L1C":
                cld_index = SELECTED_BANDS["L1C"].index("QA60")
            elif self.level == "L2A":
                cld_index = SELECTED_BANDS["L2A"].index("CLD")

            X = self.load(os.path.join(self.csvfolder, csv_file))
            culturecode, id = self.load_culturecode_and_id(os.path.join(self.csvfolder, csv_file))

            if culturecode is None or id is None:
                continue

            listcsv_statistics.append(
                dict(
                    meanQA60=np.mean(X[:, cld_index]),
                    id=id,
                    CODE_CULTU=culturecode,
                    path=os.path.join(self.csvfolder, f"{id}" + ".csv"),
                    idx=i,
                    sequencelength=len(X)
                )
            )
            i += 1

        self.index = pd.DataFrame(listcsv_statistics)
        self.index.to_csv(self.indexfile)


def get_default_transform(level):
    # padded_value = PADDING_VALUE
    sequencelength = 45

    bands = SELECTED_BANDS[level]
    if level == "L1C":
        selected_bands = ['B1', 'B10', 'B11', 'B12', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B8A', 'B9']
    elif level == "L2A":
        selected_bands = ['B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B8A', 'B11', 'B12']

    selected_band_idxs = np.array([bands.index(b) for b in selected_bands])

    def transform(x):
        # x = x[x[:, 0] != padded_value, :]  # remove padded values

        # choose selected bands
        x = x[:, selected_band_idxs] * 1e-4  # scale reflectances to 0-1

        # choose with replacement if sequencelength smaller als choose_t
        replace = False if x.shape[0] >= sequencelength else True
        idxs = np.random.choice(x.shape[0], sequencelength, replace=replace)
        idxs.sort()

        x = x[idxs]

        return torch.from_numpy(x).type(torch.FloatTensor)

    return transform


def get_default_target_transform():
    return lambda y: torch.tensor(y, dtype=torch.long)

RAW_CSV_URL = {
    2017: {
        "L1C": {
            "frh01": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L1C/frh01.zip",
            "frh02": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L1C/frh02.zip",
            "frh03": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L1C/frh03.zip",
            "frh04": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L1C/frh04.zip",
        },
        "L2A": {
            "frh01": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L2A/frh01.zip",
            "frh02": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L2A/frh02.zip",
            "frh03": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L2A/frh03.zip",
            "frh04": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L2A/frh04.zip",

        }
    },
    2018: {
        "L1C": {
            "frh01": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2018/L1C/frh01.zip",
            "frh02": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2018/L1C/frh02.zip",
            "frh03": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2018/L1C/frh03.zip",
            "frh04": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2018/L1C/frh04.zip",
        }
    }
}

INDEX_FILE_URLs = {
    2017: {
        "L1C": {
            "frh01": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L1C/frh01.csv",
            "frh02": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L1C/frh02.csv",
            "frh03": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L1C/frh03.csv",
            "frh04": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L1C/frh04.csv",
            "belle-ile": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L1C/belle-ile.csv"
        },
        "L2A": {
            "frh01": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L2A/frh01.csv",
            "frh02": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L2A/frh02.csv",
            "frh03": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L2A/frh03.csv",
            "frh04": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L2A/frh04.csv",
            "belle-ile": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L2A/belle-ile.csv"
        }
    },
    2018: {
            "L1C": {
                "frh01": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2018/L1C/frh01.csv",
                "frh02": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2018/L1C/frh02.csv",
                "frh03": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2018/L1C/frh03.csv",
                "frh04": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2018/L1C/frh04.csv",
            }
        }
}

SHP_URLs = {
    2017: {
        "frh01": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/shp/frh01.tar.gz",
        "frh02": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/shp/frh02.tar.gz",
        "frh03": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/shp/frh03.tar.gz",
        "frh04": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/shp/frh04.tar.gz",
        "belle-ile": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/shp/belle-ile.tar.gz"
    },
    2018: {
        "frh01": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2018/shp/frh01.tar.gz",
        "frh02": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2018/shp/frh02.tar.gz",
        "frh03": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2018/shp/frh03.tar.gz",
        "frh04": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2018/shp/frh04.tar.gz",
    }
}

FILESIZES = {
    2017: {
        "L1C": {
            "frh01": 2559635960,
            "frh02": 2253658856,
            "frh03": 2493572704,
            "frh04": 1555075632,
            "belle-ile": 17038944
        },
        "L2A": {
            "frh01": 987259904,
            "frh02": 803457960,
            "frh03": 890027448,
            "frh04": 639215848,
            "belle-ile": 8037952
        }
    },
    2018: {
        "L1C": {
            "frh01": 9878839310,
            "frh02": 8567550069,
            "frh03": 10196638286,
            "frh04": 2235351576 # 6270634169
        }
    }
}

H5_URLs = {
    2017: {
        "L1C": {
            "frh01": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L1C/frh01.h5.tar.gz",
            "frh02": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L1C/frh02.h5.tar.gz",
            "frh03": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L1C/frh03.h5.tar.gz",
            "frh04": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L1C/frh04.h5.tar.gz",
            "belle-ile": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L1C/belle-ile.h5.tar.gz"
        },
        "L2A": {
            "frh01": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L2A/frh01.h5.tar.gz",
            "frh02": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L2A/frh02.h5.tar.gz",
            "frh03": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L2A/frh03.h5.tar.gz",
            "frh04": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L2A/frh04.h5.tar.gz",
            "belle-ile": "https://breizhcrops.s3.eu-central-1.amazonaws.com/2017/L2A/belle-ile.h5.tar.gz"
        }
    }
}

# 9-classes used in ISPRS submission
CLASSMAPPINGURL = "https://breizhcrops.s3.eu-central-1.amazonaws.com/classmapping.csv"

# 13-classes used in ICML workshop
CLASSMAPPINGURL_ICML = "https://breizhcrops.s3.eu-central-1.amazonaws.com/classmapping_icml.csv"

CODESURL = "https://breizhcrops.s3.eu-central-1.amazonaws.com/codes.csv"

import os
import sys
import urllib
import zipfile
import tarfile
from tqdm import tqdm


def update_progress(progress):
    barLength = 20  # Modify this to change the length of the progress bar
    status = ""
    if isinstance(progress, int):
        progress = float(progress)
    if not isinstance(progress, float):
        progress = 0
        status = "error: progress var must be float\r\n"
    if progress < 0:
        progress = 0
        status = "Halt...\r\n"
    if progress >= 1:
        progress = 1
        status = "Done...\r\n"
    block = int(round(barLength * progress))
    text = "\rLoaded: [{0}] {1:.2f}% {2}".format("#" * block + "-" * (barLength - block), progress * 100, status)
    sys.stdout.write(text)
    sys.stdout.flush()


def untar(filepath):
    dirname = os.path.dirname(filepath)
    with tarfile.open(filepath, 'r:gz') as tar:
        #tar.extractall(path=dirname)
	#tar = tarfile.open(tar_file)
        for member in tar.getmembers():
            if member.isreg():  # skip if the TarInfo is not files
                member.name = os.path.basename(member.name) # remove the path by reset it
                tar.extract(member,dirname) # extract

class DownloadProgressBar(tqdm):
    def update_to(self, b=1, bsize=1, tsize=None):
        if tsize is not None:
            self.total = tsize
        self.update(b * bsize - self.n)


def download_file(url, output_path, overwrite=False):
    if url is None:
        raise ValueError("download_file: provided url is None!")

    if not os.path.exists(output_path) or overwrite:
        with DownloadProgressBar(unit='B', unit_scale=True,
                                 miniters=1, desc=url.split('/')[-1]) as t:
            urllib.request.urlretrieve(url, filename=output_path, reporthook=t.update_to)
    else:
        print(f"file exists in {output_path}. specify overwrite=True if intended")


def unzip(zipfile_path, target_dir):
    with zipfile.ZipFile(zipfile_path) as zip:
        for zip_info in zip.infolist():
            if zip_info.filename[-1] == '/':
                continue
            zip_info.filename = os.path.basename(zip_info.filename)
            zip.extract(zip_info, target_dir)