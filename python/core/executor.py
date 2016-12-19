import multiprocessing
import os
import subprocess
from time import sleep
import hashlib

from tools.misc import make_parent_dirs_if_nonexist, get_dir_without_last_slash, \
    parallel_map, check_program_exist
from core.mixin import TypeVersionEnabled
from config import get_and_assert_ffmpeg

__copyright__ = "Copyright 2016, Netflix, Inc."
__license__ = "Apache, Version 2.0"

class Executor(TypeVersionEnabled):
    """
    An Executor takes in a list of Assets, and run computations on them, and
    return a list of corresponding Results. An Executor must specify a unique
    type and version combination (by the TYPE and VERSION attribute), so that
    the Result generated by it can be uniquely identified.

    Executor is the base class for FeatureExtractor and QualityRunner, and it
    provides a number of shared housekeeping functions, including reusing
    Results, creating FIFO pipes, cleaning up log files/Results, etc.
    """

    def __init__(self,
                 assets,
                 logger,
                 fifo_mode=True,
                 delete_workdir=True,
                 result_store=None,
                 optional_dict=None,
                 optional_dict2=None,
                 ):
        """
        Use optional_dict for parameters that would impact result (e.g. model,
        patch size), and use optional_dict2 for parameters that would NOT
        impact result (e.g. path to data cache file).
        """

        TypeVersionEnabled.__init__(self)

        self.assets = assets
        self.logger = logger
        self.fifo_mode = fifo_mode
        self.delete_workdir = delete_workdir
        self.results = []
        self.result_store = result_store
        self.optional_dict = optional_dict
        self.optional_dict2 = optional_dict2

        self._assert_args()
        self._assert_assets()

    @property
    def executor_id(self):
        executor_id_ = TypeVersionEnabled.get_type_version_string(self)

        if self.optional_dict is not None and len(self.optional_dict) > 0:
            # include optional_dict info in executor_id for result store,
            # as parameters in optional_dict will impact result
            executor_id_ += '_{}'.format(
                '_'.join(
                    map(lambda k: '{k}_{v}'.format(k=k,v=self.optional_dict[k]),
                        sorted(self.optional_dict.keys()))
                )
            )

        return executor_id_
    
    def run(self, **kwargs):
        """
        Do all the computation here.
        :return:
        """
        if self.logger:
            self.logger.info(
                "For each asset, if {type} result has not been generated, run "
                "and generate {type} result...".format(type=self.executor_id))

        if 'parallelize' in kwargs:
            parallelize = kwargs['parallelize']
        else:
            parallelize = False

        if parallelize:
            # create locks for unique assets (uniqueness is identified by str(asset))
            map_asset_lock = {}
            locks = []
            for asset in self.assets:
                asset_str = str(asset)
                if asset_str not in map_asset_lock:
                    map_asset_lock[asset_str] = multiprocessing.Lock()
                locks.append(map_asset_lock[asset_str])

            # pack key arguments to be used as inputs to map function
            list_args = []
            for asset, lock in zip(self.assets, locks):
                list_args.append(
                    [asset, lock])

            def _run(asset_lock):
                asset, lock = asset_lock
                lock.acquire()
                result = self._run_on_asset(asset)
                lock.release()
                return result

            self.results = parallel_map(_run, list_args)
        else:
            self.results = map(self._run_on_asset, self.assets)

    def remove_results(self):
        """
        Remove all relevant Results stored in ResultStore, which is specified
        at the constructor.
        :return:
        """
        for asset in self.assets:
            self._remove_result(asset)

    def _assert_args(self):

        pass

    def _assert_assets(self):

        for asset in self.assets:
            self._assert_an_asset(asset)

        # ===============================================
        # after using locks in run_executors_in_parallel,
        # no longer need constraint below
        # ===============================================
        # list_dataset_contentid_assetid = \
        #     map(lambda asset: (asset.dataset, asset.content_id, asset.asset_id),
        #         self.assets)
        # assert len(list_dataset_contentid_assetid) == \
        #        len(set(list_dataset_contentid_assetid)), \
        #     "Triplet of dataset, content_id and asset_id must be unique for each asset."

        pass

    @classmethod
    def _assert_an_asset(cls, asset):

        # if quality width/height do not to agree with ref/dis width/height,
        # must rely on ffmpeg for scaling
        if asset.quality_width_height != asset.ref_width_height \
                or asset.quality_width_height != asset.dis_width_height:
            get_and_assert_ffmpeg()

        # if crop_cmd or pad_cmd is specified, make sure quality_width and
        # quality_height are EXPLICITLY specified in asset_dict
        if asset.crop_cmd is not None:
            assert 'quality_width' in asset.asset_dict and 'quality_height' in asset.asset_dict, \
                'If crop_cmd is specified, must also EXPLICITLY specify quality_width and quality_height.'
        if asset.pad_cmd is not None:
            assert 'quality_width' in asset.asset_dict and 'quality_height' in asset.asset_dict, \
                'If pad_cmd is specified, must also EXPLICITLY specify quality_width and quality_height.'

        pass

    def _wait_for_workfiles(self, asset):
        # wait til workfile paths being generated
        # FIXME: use proper mutex (?)
        for i in range(10):
            if os.path.exists(asset.ref_workfile_path) and \
                    os.path.exists(asset.dis_workfile_path):
                break
            sleep(0.1)
        else:
            raise RuntimeError(
                "ref or dis video workfile path {ref} or {dis} is missing.".
                    format(ref=asset.ref_workfile_path,
                           dis=asset.dis_workfile_path)
            )

    def _prepare_log_file(self, asset):

        log_file_path = self._get_log_file_path(asset)

        # if parent dir doesn't exist, create
        make_parent_dirs_if_nonexist(log_file_path)

        # add runner type and version
        with open(log_file_path, 'wt') as log_file:
            log_file.write("{type_version_str}\n\n".format(
                type_version_str=self.get_cozy_type_version_string()))

    def _assert_paths(self, asset):
        assert os.path.exists(asset.ref_path), \
            "Reference path {} does not exist.".format(asset.ref_path)
        assert os.path.exists(asset.ref_path), \
            "Distorted path {} does not exist.".format(asset.dis_path)

    def _run_on_asset(self, asset):
        # Wraper around the essential function _generate_result, to
        # do housekeeping work including 1) asserts of asset, 2) skip run if
        # log already exist, 3) creating fifo, 4) delete work file and dir

        if self.result_store:
            result = self.result_store.load(asset, self.executor_id)
        else:
            result = None

        # if result can be retrieved from result_store, skip log file
        # generation and reading result from log file, but directly return
        # return the retrieved result
        if result is not None:
            if self.logger:
                self.logger.info('{id} result exists. Skip {id} run.'.
                                 format(id=self.executor_id))
        else:

            if self.logger:
                self.logger.info('{id} result does\'t exist. Perform {id} '
                                 'calculation.'.format(id=self.executor_id))

            # at this stage, it is certain that asset.ref_path and
            # asset.dis_path will be used. must early determine that
            # they exists
            self._assert_paths(asset)

            # if no rescaling is involved, directly work on ref_path/dis_path,
            # instead of opening workfiles
            self._set_asset_use_path_as_workpath(asset)

            # remove workfiles if exist (do early here to avoid race condition
            # when ref path and dis path have some overlap)
            if asset.use_path_as_workpath:
                # do nothing
                pass
            else:
                self._close_ref_workfile(asset)
                self._close_dis_workfile(asset)

            log_file_path = self._get_log_file_path(asset)
            make_parent_dirs_if_nonexist(log_file_path)

            if asset.use_path_as_workpath:
                # do nothing
                pass
            else:
                if self.fifo_mode:
                    ref_p = multiprocessing.Process(target=self._open_ref_workfile,
                                                    args=(asset, True))
                    dis_p = multiprocessing.Process(target=self._open_dis_workfile,
                                                    args=(asset, True))
                    ref_p.start()
                    dis_p.start()
                    self._wait_for_workfiles(asset)
                else:
                    self._open_ref_workfile(asset, fifo_mode=False)
                    self._open_dis_workfile(asset, fifo_mode=False)

            self._prepare_log_file(asset)

            self._generate_result(asset)

            # clean up workfiles
            if self.delete_workdir:
                if asset.use_path_as_workpath:
                    # do nothing
                    pass
                else:
                    self._close_ref_workfile(asset)
                    self._close_dis_workfile(asset)

            if self.logger:
                self.logger.info("Read {id} log file, get scores...".
                                 format(type=self.executor_id))

            # collect result from each asset's log file
            result = self._read_result(asset)

            # save result
            if self.result_store:
                self.result_store.save(result)

            # clean up workdir and log files in it
            if self.delete_workdir:

                # remove log file
                self._remove_log(asset)

                # remove dir
                log_file_path = self._get_log_file_path(asset)
                log_dir = get_dir_without_last_slash(log_file_path)
                try:
                    os.rmdir(log_dir)
                except OSError as e:
                    if e.errno == 39: # [Errno 39] Directory not empty
                        # VQM could generate an error file with non-critical
                        # information like: '3 File is longer than 15 seconds.
                        # Results will be calculated using first 15 seconds
                        # only.' In this case, want to keep this
                        # informational file and pass
                        pass

        result = self._post_process_result(result)

        return result

    @staticmethod
    def _set_asset_use_path_as_workpath(asset):
        # if no rescaling or croping or padding is involved, directly work on
        # ref_path/dis_path, instead of opening workfiles
        if asset.quality_width_height == asset.ref_width_height \
                and asset.quality_width_height == asset.dis_width_height\
                and asset.crop_cmd is None\
                and asset.pad_cmd is None:
            asset.use_path_as_workpath = True

    @classmethod
    def _post_process_result(cls, result):
        # do nothing, wait to be overridden
        return result

    def _get_log_file_path(self, asset):
        return "{workdir}/{executor_id}_{str}".format(
            workdir=asset.workdir, executor_id=self.executor_id,
            str=hashlib.sha1(str(asset)).hexdigest())

    # ===== workfile =====

    def _open_ref_workfile(self, asset, fifo_mode):
        # For now, only works for YUV format -- all need is to copy from ref
        # file to ref workfile

        # only need to open ref workfile if the path is different from ref path
        assert asset.use_path_as_workpath == False \
               and asset.ref_path != asset.ref_workfile_path

        # if fifo mode, mkfifo
        if fifo_mode:
            os.mkfifo(asset.ref_workfile_path)

        quality_width, quality_height = asset.quality_width_height
        yuv_type = asset.yuv_type
        resampling_type = asset.resampling_type

        width, height = asset.ref_width_height
        src_fmt_cmd = self._get_src_fmt_cmd(asset, height, width)

        crop_cmd = self._get_crop_cmd(asset)
        pad_cmd = self._get_pad_cmd(asset)

        ffmpeg_cmd = '{ffmpeg} {src_fmt_cmd} -i {src} -an -vsync 0 ' \
                     '-pix_fmt {yuv_type} -vf {crop_cmd}{pad_cmd}scale={width}x{height} -f rawvideo ' \
                     '-sws_flags {resampling_type} -y {dst}'.format(
            ffmpeg=get_and_assert_ffmpeg(),
            src=asset.ref_path, dst=asset.ref_workfile_path,
            width=quality_width, height=quality_height,
            src_fmt_cmd=src_fmt_cmd,
            crop_cmd=crop_cmd,
            pad_cmd=pad_cmd,
            yuv_type=yuv_type,
            resampling_type=resampling_type)
        if self.logger:
            self.logger.info(ffmpeg_cmd)
        subprocess.call(ffmpeg_cmd, shell=True)

    def _open_dis_workfile(self, asset, fifo_mode):
        # For now, only works for YUV format -- all need is to copy from dis
        # file to dis workfile

        # only need to open dis workfile if the path is different from dis path
        assert asset.use_path_as_workpath == False \
               and asset.dis_path != asset.dis_workfile_path

        # if fifo mode, mkfifo
        if fifo_mode:
            os.mkfifo(asset.dis_workfile_path)

        quality_width, quality_height = asset.quality_width_height
        yuv_type = asset.yuv_type
        resampling_type = asset.resampling_type

        width, height = asset.dis_width_height
        src_fmt_cmd = self._get_src_fmt_cmd(asset, height, width)

        crop_cmd = self._get_crop_cmd(asset)
        pad_cmd = self._get_pad_cmd(asset)

        ffmpeg_cmd = '{ffmpeg} {src_fmt_cmd} -i {src} -an -vsync 0 ' \
                     '-pix_fmt {yuv_type} -vf {crop_cmd}{pad_cmd}scale={width}x{height} -f rawvideo ' \
                     '-sws_flags {resampling_type} -y {dst}'.format(
            ffmpeg=get_and_assert_ffmpeg(),
            src=asset.dis_path, dst=asset.dis_workfile_path,
            width=quality_width, height=quality_height,
            src_fmt_cmd=src_fmt_cmd,
            crop_cmd=crop_cmd,
            pad_cmd=pad_cmd,
            yuv_type=yuv_type,
            resampling_type=resampling_type)
        if self.logger:
            self.logger.info(ffmpeg_cmd)
        subprocess.call(ffmpeg_cmd, shell=True)

    def _get_src_fmt_cmd(self, asset, height, width):
        src_fmt_cmd = '-f rawvideo -pix_fmt {yuv_fmt} -s {width}x{height}'. \
            format(yuv_fmt=asset.yuv_type, width=width, height=height)
        return src_fmt_cmd

    def _get_crop_cmd(self, asset):
        crop_cmd = "crop={},".format(
            asset.crop_cmd) if asset.crop_cmd is not None else ""
        return crop_cmd

    def _get_pad_cmd(self, asset):
        pad_cmd = "pad2={},".format(
            asset.pad_cmd) if asset.pad_cmd is not None else ""
        return pad_cmd

    @staticmethod
    def _close_ref_workfile(asset):

        # only need to close ref workfile if the path is different from ref path
        assert asset.use_path_as_workpath is False \
               and asset.ref_path != asset.ref_workfile_path

        # caution: never remove ref file!!!!!!!!!!!!!!!
        if os.path.exists(asset.ref_workfile_path):
            os.remove(asset.ref_workfile_path)

    @staticmethod
    def _close_dis_workfile(asset):

        # only need to close dis workfile if the path is different from dis path
        assert asset.use_path_as_workpath is False \
               and asset.dis_path != asset.dis_workfile_path

        # caution: never remove dis file!!!!!!!!!!!!!!
        if os.path.exists(asset.dis_workfile_path):
            os.remove(asset.dis_workfile_path)

    def _remove_log(self, asset):
        log_file_path = self._get_log_file_path(asset)
        if os.path.exists(log_file_path):
            os.remove(log_file_path)

    def _remove_result(self, asset):
        if self.result_store:
            self.result_store.delete(asset, self.executor_id)


def run_executors_in_parallel(executor_class,
                              assets,
                              fifo_mode=True,
                              delete_workdir=True,
                              parallelize=True,
                              logger=None,
                              result_store=None,
                              optional_dict=None,
                              optional_dict2=None,
                              ):
    """
    Run multiple Executors in parallel.
    """

    # construct an executor object just to call _assert_assets() only
    _ = executor_class(assets,
                 logger,
                 fifo_mode=fifo_mode,
                 delete_workdir=True,
                 result_store=result_store,
                 optional_dict=optional_dict,
                 optional_dict2=optional_dict2)

    # create locks for unique assets (uniqueness is identified by str(asset))
    map_asset_lock = {}
    locks = []
    for asset in assets:
        asset_str = str(asset)
        if asset_str not in map_asset_lock:
            map_asset_lock[asset_str] = multiprocessing.Lock()
        locks.append(map_asset_lock[asset_str])

    # pack key arguments to be used as inputs to map function
    list_args = []
    for asset, lock in zip(assets, locks):
        list_args.append(
            [executor_class, asset, fifo_mode, delete_workdir,
             result_store, optional_dict, optional_dict2, lock])

    def run_executor(args):
        executor_class, asset, fifo_mode, delete_workdir, \
        result_store, optional_dict, optional_dict2, lock = args
        lock.acquire()
        executor = executor_class([asset], None, fifo_mode, delete_workdir,
                                  result_store, optional_dict, optional_dict2)
        executor.run()
        lock.release()
        return executor

    # run
    if parallelize:
        executors = parallel_map(run_executor, list_args, processes=None)
    else:
        executors = map(run_executor, list_args)

    # aggregate results
    results = [executor.results[0] for executor in executors]

    return executors, results

