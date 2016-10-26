"""
Run the uncoverml pipeline for clustering, supervised learning and prediction.

.. program-output:: uncoverml --help
"""
import logging
import pickle
import resource
import tempfile
from os.path import isfile

import click
import numpy as np
import uncoverml as ls
import uncoverml.cluster
import uncoverml.config
import uncoverml.features
import uncoverml.geoio
import uncoverml.learn
import uncoverml.logging
import uncoverml.mpiops
import uncoverml.predict
import uncoverml.validate
from uncoverml import resampling

log = logging.getLogger(__name__)


@click.group()
@click.option('-v', '--verbosity',
              type=click.Choice(['DEBUG', 'INFO', 'WARNING', 'ERROR']),
              default='INFO', help='Level of logging')
def cli(verbosity):
    ls.logging.configure(verbosity)


def run_crossval(x_all, targets_all, config):
    crossval_results = ls.validate.local_crossval(x_all,
                                                      targets_all, config)
    ls.mpiops.run_once(ls.geoio.export_crossval, crossval_results, config)


def resample_shapefile(config):
    shapefile = config.target_file

    # sample shapefile
    if not config.resample:
        return shapefile
    else:
        log.info('Stripping shapefile of unnecessary attributes')
        temp_shapefile = tempfile.mktemp(suffix='.shp')

        if config.resample == 'value':
            log.info("resampling shape file "
                     "based on '{}' values".format(config.target_property))
            resampling.resample_shapefile(shapefile,
                                          temp_shapefile,
                                          target_field=config.target_property,
                                          **config.resample_args
                                          )
        else:
            assert config.resample == 'spatial', \
                "resample must be 'value' or 'spatial'"
            log.info("resampling shape file spatially")

            resampling.resample_shapefile_spatially(
                shapefile, temp_shapefile,
                target_field=config.target_property,
                **config.resample_args)

        return temp_shapefile


@cli.command()
@click.argument('pipeline_file')
@click.option('-p', '--partitions', type=int, default=1,
              help='divide each node\'s data into this many partitions')
def learn(pipeline_file, partitions):
    config = ls.config.Config(pipeline_file)
    config.n_subchunks = partitions
    if config.n_subchunks > 1:
        log.info("Memory constraint forcing {} iterations "
                 "through data".format(config.n_subchunks))
    else:
        log.info("Using memory aggressively: dividing all data between nodes")

    config.target_file = resample_shapefile(config)
    # Make the targets
    targets = ls.geoio.load_targets(shapefile=config.target_file,
                                    targetfield=config.target_property)
    # We're doing local models at the moment
    targets_all = ls.targets.gather_targets(targets, node=0)

    # Get the image chunks and their associated transforms
    image_chunk_sets = ls.geoio.image_feature_sets(targets, config)
    transform_sets = [k.transform_set for k in config.feature_sets]

    if config.rank_features:
        measures, features, scores = ls.validate.local_rank_features(
            image_chunk_sets,
            transform_sets,
            targets_all,
            config)
        ls.mpiops.run_once(ls.geoio.export_feature_ranks, measures, features,
                           scores, config)

    # need to add cubist cols to config.algorithm_args
    x = ls.features.transform_features(image_chunk_sets, transform_sets,
                                       config.final_transform, config)
    # learn the model
    # local models need all data
    x_all = ls.features.gather_features(x, node=0)

    if config.cross_validate:
        run_crossval(x_all, targets_all, config)

    log.info("Learning full {} model".format(config.algorithm))
    model = ls.learn.local_learn_model(x_all, targets_all, config)
    ls.mpiops.run_once(ls.geoio.export_model, model, config)
    log.info("Finished! Total mem = {:.1f} GB".format(_total_gb()))


@cli.command()
@click.argument('pipeline_file')
@click.option('-s', '--subsample_fraction', type=float, default=1.0,
              help='only use this fraction of the data for learning classes')
def cluster(pipeline_file, subsample_fraction):
    config = ls.config.Config(pipeline_file)
    config.subsample_fraction = subsample_fraction
    if config.subsample_fraction < 1:
        log.info("Memory contstraint: using {:2.2f}%"
                 " of pixels".format(config.subsample_fraction * 100))
    else:
        log.info("Using memory aggressively: dividing all data between nodes")

    if config.semi_supervised:
        semisupervised(config)
    else:
        unsupervised(config)
    log.info("Finished! Total mem = {:.1f} GB".format(_total_gb()))


def semisupervised(config):

    # make sure we're clear that we're clustering
    config.algorithm = config.clustering_algorithm
    config.cubist = False
    # Get the taregts
    targets = ls.geoio.load_targets(shapefile=config.class_file,
                                    targetfield=config.class_property)

    # Get the image chunks and their associated transforms
    image_chunk_sets = ls.geoio.semisupervised_feature_sets(targets, config)
    transform_sets = [k.transform_set for k in config.feature_sets]

    x = ls.features.transform_features(image_chunk_sets, transform_sets,
                                       config.final_transform, config)

    x, classes = ls.features.remove_missing(x, targets)
    indices = np.arange(classes.shape[0], dtype=int)

    k = ls.cluster.compute_n_classes(classes, config)
    model = ls.cluster.KMeans(k, config.oversample_factor)
    log.info("Clustering image")
    model.learn(x, indices, classes)
    ls.mpiops.run_once(ls.geoio.export_cluster_model, model, config)


def unsupervised(config):
    # make sure we're clear that we're clustering
    config.algorithm = config.clustering_algorithm
    config.cubist = False
    # Get the image chunks and their associated transforms
    image_chunk_sets = ls.geoio.unsupervised_feature_sets(config)
    transform_sets = [k.transform_set for k in config.feature_sets]

    x = ls.features.transform_features(image_chunk_sets, transform_sets,
                                       config.final_transform, config)

    x, _ = ls.features.remove_missing(x)
    k = config.n_classes
    model = ls.cluster.KMeans(k, config.oversample_factor)
    log.info("Clustering image")
    model.learn(x)
    ls.mpiops.run_once(ls.geoio.export_cluster_model, model, config)


@cli.command()
@click.argument('model_or_cluster_file')
@click.option('-p', '--partitions', type=int, default=1,
              help='divide each node\'s data into this many partitions')
@click.option('-m', '--mask', type=str, default='',
              help='mask file used to limit prediction area')
@click.option('-r', '--retain', type=int, default=None,
              help='mask values where to predict')
def predict(model_or_cluster_file, partitions, mask, retain):

    with open(model_or_cluster_file, 'rb') as f:
        state_dict = pickle.load(f)

    model = state_dict["model"]
    config = state_dict["config"]

    config.mask = mask if mask else config.mask
    if config.mask:
        config.retain = retain if retain else config.retain

        if not isfile(config.mask):
            config.mask = ''
            log.info('A mask was provided, but the file does not exist on '
                     'disc or is not a file.')

    config.n_subchunks = partitions
    if config.n_subchunks > 1:
        log.info("Memory contstraint forcing {} iterations "
                 "through data".format(config.n_subchunks))
    else:
        log.info("Using memory aggressively: dividing all data between nodes")

    image_shape, image_bbox = ls.geoio.get_image_spec(model, config)

    outfile_tif = config.name + "_" + config.algorithm
    image_out = ls.geoio.ImageWriter(image_shape, image_bbox, outfile_tif,
                                     config.n_subchunks, config.output_dir,
                                     band_tags=model.get_predict_tags())

    for i in range(config.n_subchunks):
        log.info("starting to render partition {}".format(i+1))
        ls.predict.render_partition(model, i, image_out, config)
    log.info("Finished! Total mem = {:.1f} GB".format(_total_gb()))


def _total_gb():
    # given in KB so convert
    my_usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024**2)
    # total_usage = mpiops.comm.reduce(my_usage, root=0)
    total_usage = ls.mpiops.comm.allreduce(my_usage)
    return total_usage
