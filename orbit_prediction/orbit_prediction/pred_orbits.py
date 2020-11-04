# Copyright 2020 IBM Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import logging
import numpy as np
import pandas as pd
import datetime as dt
from functools import partial
import orbit_prediction.ml_model as ml_model
import orbit_prediction.spacetrack_etl as st
import orbit_prediction.build_training_data as td


logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"))
logger = logging.getLogger(__name__)


def get_latest_orbit_data(space_track_user,
                          space_track_password,
                          norad_ids=None):
    """Fetches the latest TLE data from Space Track

    :param space_track_user: The user name for the Space Track account
    :type space_track_user: str

    :param space_track_password: The password for the Space Track account
    :type space_track_password:

    :param norad_ids: An optional list of NORAD IDs to fetch the TLEs
        for.  If NORAD IDs are not provided then data will be fetched
        for all ASOs in LEO.
    :type norad_ids: [str]

    :return: A DataFrame containing the latest TLE data for the requested ASOs
    :rtype: pandas.DataFrame
    """
    stc = st.build_space_track_client(space_track_user, space_track_password)
    latest_orbit_data = st.build_leo_df(stc,
                                        norad_ids=norad_ids,
                                        last_n_days=30,
                                        only_latest=True)
    return latest_orbit_data


def predict_orbit(row, pred_start, timesteps):
    """Uses a physical model to predict the orbital state vectors
    for each provided timestep into the future.

    :param row: The DataFrame row to make the orbit predictions for
    :type row: pandas.Series

    :param pred_start: The timestamp at which to start the prediction window
    :type pred_start: pandas.Timestamp

    :param timestep: A list of seconds into the future to predict the orbit
       for
    :type timestep: [float]

    :return: The elapsed seconds from `pred_start` and the predicted
        state vectors for each timestep
    :rtype: np.array
    """
    orbit = td.build_orbit(row)
    if row.epoch == pred_start:
        # The row's epoch is the same as the prediction window start timestamp
        # so we don't need to fast forward the first prediction.
        timesteps = [0] + timesteps
    else:
        # The row's epoch is behind the prediction window start timestamp so we
        # calculate the number of seconds we need to propagate the orbit to
        # have the epoch be the same as the prediction start time.
        offset = (pred_start - row.epoch).total_seconds()
        timesteps = [offset] + timesteps

    ts_preds = []
    elapsed_seconds = 0
    for ts in timesteps:
        orbit_propagator = td.build_orbit_propagator(orbit,
                                                     return_orbit=True)
        orbit, orbit_pred = orbit_propagator(ts)
        elapsed_seconds += ts
        # Create a numpy array where the first value is number of seconds
        # that have elpased since the prediction window's start time and then
        # the next six values are the predicted orbital state vector.
        ts_pred = np.insert(orbit_pred, 0, elapsed_seconds, axis=0)
        ts_preds.append(ts_pred)
    return np.stack(ts_preds, axis=0)


def predict_orbits(df, ml_models, n_days, timestep):
    """Use a physical model to predict the future orbits of all ASOs in the
    provided DataFrame, then use ML models to predict the error in the physics
    predictions, and finally adjust the physical predictions based on the error
    estimates.

    :param df: The latest TLE data for the ASOs to predict the orbits of
    :type df: pandas.DataFrame

    :param ml_models: The ML models to use to estimate the error for each
        component of the predicted state vector
    :type ml_models: [xgboost.XGBRegressor]

    :param n_days: The number of days into the future to predict orbits for
    :type n_days: int

    :param timestep: The frequency in seconds to make orbital predictions at
    :type timestep: float

    :return: The input DataFrame with the physical orbit predictions, the
        estimated errors, and the corrected orbit predictions added
        as columns
    :rtype: pandas.DataFrame
    """
    # Use the latest epoch in the dataset as the start of the prediction window
    pred_start = df.epoch.max()
    pred_end = pred_start + dt.timedelta(days=n_days)
    df['pred_start_dt'] = pred_start
    df['pred_end_dt'] = pred_end
    # Get the total amount of seconds in the prediction window
    pred_window_seconds = (pred_end - pred_start).total_seconds()
    # Calculate how many predictions we will make based on the
    # the length of the prediction window and the timestep
    n_pred_intervals = int(pred_window_seconds / timestep) - 1
    timesteps = [timestep]*n_pred_intervals

    orbit_predictor = partial(predict_orbit,
                              pred_start=pred_start,
                              timesteps=timesteps)

    def err_est(preds):
        return ml_model.predict_err(ml_models, preds)

    logger.info('Predicting Orbits...')
    df['physics_preds'] = df.apply(orbit_predictor, axis=1)
    logger.info('Estimating physics errors...')
    df['ml_err_preds'] = df.physics_preds.apply(err_est)

    # Convert the physical predictions to a numpy 3D array and drop
    # the first element of the last axis which is the elapsed time
    physics_array = np.stack(df.physics_preds.to_numpy())[:, :, 1:]
    ml_array = np.stack(df.ml_err_preds.to_numpy())
    # the corrected predictions are the physics predictions with the
    # estimated errors subtracted off
    corrected_preds = physics_array - ml_array
    # Convert the 3D numpy array into a list of 2D arrays
    orbit_preds = [corrected_preds[i]
                   for i
                   in range(corrected_preds.shape[0])]
    df['orbit_preds'] = pd.Series(orbit_preds)
    return df


def run(args):
    """Combine physic and ML models to predict future orbits based on parameters
    specified by the CLI.

    :param args: The command line arguments
    :type args: argparse.Namespace
    """
    latest_orbit_data = get_latest_orbit_data(args.st_user,
                                              args.st_password,
                                              norad_ids=args.norad_ids)
    logger.info('Loading ML Models...')
    ml_models = ml_model.load_models(args.ml_model_dir)

    orbit_pred_df = predict_orbits(latest_orbit_data,
                                   ml_models,
                                   n_days=args.n_days,
                                   timestep=args.timestep)
    logger.info('Serializing Results...')
    orbit_pred_df.to_pickle(args.output_path)
