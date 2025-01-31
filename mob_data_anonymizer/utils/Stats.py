from collections import defaultdict
import logging
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime
from math import sqrt

import numpy as np
import pandas as pd
import xgboost as xgb
from pandas import DateOffset
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split
from sklearn.utils import shuffle
from skmob.tessellation import tilers
from tqdm import tqdm
import warnings

warnings.filterwarnings('ignore')
from mob_data_anonymizer.entities.Dataset import Dataset


class Stats:

    def __init__(self, original: Dataset, anonymized: Dataset):
        self.original_dataset = original
        self.anonymized_dataset = anonymized

    def get_number_of_removed_trajectories(self):
        return len(self.original_dataset) - len(self.anonymized_dataset)

    def get_number_of_removed_locations(self):
        return self.original_dataset.get_number_of_locations() - self.anonymized_dataset.get_number_of_locations()

    def get_perc_of_removed_trajectories(self):
        return self.get_number_of_removed_trajectories() / len(self.original_dataset)

    def get_perc_of_removed_locations(self):
        return self.get_number_of_removed_locations() / self.original_dataset.get_number_of_locations()

    def get_rsme(self, distance):
        # TODO: Y como se mide la diferencia cuando una trayectoría ha sido eliminada?
        distance.distance_matrix = defaultdict(dict)
        anom_trajectories = {}
        for t in self.anonymized_dataset.trajectories:
            anom_trajectories[t.id] = t
        dist = 0.0
        for t_ori in self.original_dataset.trajectories:
            if t_ori.id in anom_trajectories:
                t_anon = anom_trajectories[t_ori.id]
                d = distance.compute(t_ori, t_anon)
                dist += pow(d, 2)
        dist /= len(self.anonymized_dataset)
        dist = sqrt(dist)

        return dist

    def get_record_linkage(self, distance):
        logging.info("Calculating privacy metric (record linkage)")
        control = {}
        ids = {}
        for trajectory in self.original_dataset.trajectories:
            count = control.get(trajectory)
            if count is not None:
                count += 1
            else:
                count = 1
                ids[trajectory] = []
            control[trajectory] = count
            ids[trajectory].append(trajectory.id)

        total_prob = 0
        min_traj = None
        for traj_anom in tqdm(self.anonymized_dataset.trajectories):
            min_dist = float('inf')
            for traj_ori in self.original_dataset.trajectories:
                dist = distance.compute_without_map(traj_ori, traj_anom)
                if dist < min_dist:
                    min_dist = dist
                    min_traj = traj_ori
            ids_group = ids[min_traj]
            if traj_anom.id in ids_group:
                count = control[min_traj]
                partial = 1 / count
                total_prob += partial

        return (total_prob / len(self.original_dataset)) * 100

    def get_fast_record_linkage(self, distance, window_size=None):
        WINDOW_SIZE = 1.0  # it indicates the % of the num of trajectories in the dataset
        if window_size is None:
            window_size = (len(self.original_dataset) * WINDOW_SIZE) / 100
            if window_size < 1.0:
                window_size = len(self.original_dataset)
        logging.info("Calculating fast record linkage (disclosure risk), window size = " + str(window_size))
        distance.compute_reference_trajectory()

        control = {}
        ids = {}
        for trajectory in self.original_dataset.trajectories:
            trajectory.distance_to_reference_trajectory = \
                distance.compute_distance_to_reference_trajectory(trajectory)
            count = control.get(trajectory)
            if count is not None:
                count += 1
            else:
                count = 1
                ids[trajectory] = []
            control[trajectory] = count
            ids[trajectory].append(trajectory.id)

        self.original_dataset.trajectories.sort(key=lambda x: x.distance_to_reference_trajectory)
        distances = [trajectory.distance_to_reference_trajectory for trajectory in self.original_dataset.trajectories]
        min_traj = None
        total_prob = 0
        for trajectory_anom in tqdm(self.anonymized_dataset.trajectories):
            trajectory_anom.distance_to_reference_trajectory = \
                distance.compute_distance_to_reference_trajectory(trajectory_anom)
            closest_trajectories = Stats.__take_closest_window(distances,
                                                               trajectory_anom.distance_to_reference_trajectory,
                                                               window_size)
            min_dist = float('inf')
            for pos in closest_trajectories:
                trajectory = self.original_dataset.trajectories[pos]
                dist = distance.compute_without_map(trajectory, trajectory_anom)
                if dist < min_dist:
                    min_dist = dist
                    min_traj = trajectory
            ids_group = ids[min_traj]
            if trajectory_anom.id in ids_group:
                count = control[min_traj]
                partial = 1 / count
                total_prob += partial

        # rearranging
        self.original_dataset.trajectories.sort(key=lambda x: x.id)

        return (total_prob / len(self.original_dataset)) * 100

    def __take_closest(myList, myNumber):
        """
        Assumes myList is sorted. Returns the index of the closest value to myNumber.
        If two numbers are equally close, return the index of the smallest number.
        :param (list) myList: the list of values
        :param (float) myNumber: The number to be searched
        :return: The index of myList of the closest value to myNumber
        :rtype: int
        """
        pos = bisect_left(myList, myNumber)
        if pos == 0:
            return pos
            # return myList[0]
        if pos == len(myList):
            return pos
            # return m/yList[-1]
        before = myList[pos - 1]
        after = myList[pos]
        if after - myNumber < myNumber - before:
            # return after
            return pos
        else:
            # return before
            return pos - 1

    def __take_closest_window(myList, myNumber, window_size):
        """
        Assumes myList is sorted. Returns a list of positions of my list with size window_size.
        The window_size positions of the closest values in mylist to myNumber.
        :param (list) myList: the list of values
        :param (float) myNumber: The number to be searched
        :param (float) window_size: The number of positions to be returned
        :return: The list of indexes of myList of the closest value to myNumber
        :rtype: list of int
        """
        pos = Stats.__take_closest(myList, myNumber)
        cut = int(window_size / 2)
        rest_before = 0
        rest_after = 0
        pos_before = pos - cut
        if window_size % 2 == 0:
            pos_before += 1
        if pos_before < 0:
            rest_before = pos_before * -1
            pos_before = 0
        pos_after = pos + cut
        if pos_after > len(myList) - 1:
            rest_after = pos_after - (len(myList) - 1)
            pos_after = len(myList) - 1
        pos_before -= rest_after
        pos_after += rest_before

        return [x for x in range(pos_before, pos_after + 1)]

    def get_propensity_score(self, tiles_size=200, time_interval=None):

        # Compute tessellation and data ranges for the original dataset
        logging.info(f"Tessellation")

        tessellation = tilers.tiler.get("squared", base_shape=self.original_dataset.get_bounding_box(),
                                        meters=tiles_size)
        tessellation['tile_ID'] = pd.to_numeric(tessellation['tile_ID'])

        datetime_ranges = None
        if time_interval:
            logging.info("Time tessellation")
            offset = DateOffset(seconds=time_interval)

            min_datetime = datetime.fromtimestamp(self.original_dataset.get_min_timestamp(),
                                                  self.original_dataset.timezone)
            max_datetime = datetime.fromtimestamp(self.original_dataset.get_max_timestamp(),
                                                  self.original_dataset.timezone)

            datetime_ranges = pd.date_range(min_datetime, max_datetime, freq=offset)

        original_sequences = self.__compute_trajectory_sequences(self.original_dataset, tessellation, datetime_ranges)
        print('trajectory sequences computed')
        anonymized_sequences = self.__compute_trajectory_sequences(self.anonymized_dataset, tessellation,
                                                                   datetime_ranges)

        print('trajectory sequences computed 2')

        # Check the max len of the sequences and repadding if necessary
        max_orig = max([len(original_sequences[i]) for i in original_sequences.keys()])
        max_anon = max([len(anonymized_sequences[i]) for i in anonymized_sequences.keys()])

        if max_orig > max_anon:
            for i in anonymized_sequences.keys():
                anonymized_sequences[i] = [0] * (max_orig - len(anonymized_sequences[i])) + anonymized_sequences[i]

        if max_anon > max_orig:
            for i in original_sequences.keys():
                original_sequences[i] = [0] * (max_anon - len(original_sequences[i])) + original_sequences[i]

        df1 = pd.DataFrame([original_sequences[key] for key in original_sequences.keys()])
        clas = [0] * len(df1)
        df1['clas'] = clas
        df2 = pd.DataFrame([anonymized_sequences[key] for key in anonymized_sequences.keys()])
        clas = [1] * len(df2)
        df2['clas'] = clas
        df = df1.append(df2, ignore_index=True)
        df = shuffle(df)
        # print(df)

        train, test = train_test_split(df)
        X_train = train.drop(columns=['clas'])
        y_train = train['clas']
        X_test = test.drop(columns=['clas'])
        y_test = test['clas']
        X_all = df.drop(columns=['clas'])
        y_all = df['clas']

        # print(pd.DataFrame(X_all).shape)

        # print(pd.DataFrame(X_train).shape)
        # print(pd.DataFrame(y_train).shape)
        # print(pd.DataFrame(X_test).shape)
        # print(pd.DataFrame(y_test).shape)

        # model = LogisticRegression()
        # model = GradientBoostingClassifier()
        # model = xgb.XGBClassifier(n_jobs=multiprocessing.cpu_count() // 2)
        # clf = GridSearchCV(model, {'max_depth': [2, 4, 6], 'n_estimators': [50, 100, 200]}, verbose=1, n_jobs=2)
        # clf.fit(X_train, y_train)
        # print(clf.best_score_)
        # print(clf.best_params_)
        # sys.exit(0)

        # clf = LazyClassifier(verbose=0, ignore_warnings=True, custom_metric=None)
        # models, predictions = clf.fit(X_train, X_test, y_train, y_test)
        # print(models)
        # sys.exit(0)

        # model = LogisticRegression()
        model = xgb.XGBClassifier()
        # model = ExtraTreesClassifier()

        model.fit(X_train, y_train)
        preds_train = model.predict(X_train)
        preds_test = model.predict(X_test)
        print('accuracy in train:', accuracy_score(y_train, preds_train))
        print('accuracy in test:', accuracy_score(y_test, preds_test))

        preds_all = model.predict(X_all)
        print('accuracy in all:', accuracy_score(y_all, preds_all))

        # probs = np.max(model.predict_proba(X_all), axis=1)
        probs = model.predict_proba(X_all)[:,1]
        v = 0
        for prob in probs:
            p = (prob - 0.5) ** 2
            v += p
        v /= len(probs)

        return v * 4.0

    def __compute_trajectory_sequences(self, dataset, tessellation, datetime_ranges=None):

        tdf = dataset.to_tdf()

        max_tile_id = tessellation['tile_ID'].max()
        print(f'MAX tile: {max_tile_id}')
        # Map locations to spatial tiles
        st_tdf = tdf.mapping(tessellation, remove_na=True)

        # Modify tiles_id based on time
        if datetime_ranges is not None:
            st_tdf['tile_ID'] = st_tdf.apply(
                lambda row: row['tile_ID'] + (
                        max_tile_id * (bisect_left(datetime_ranges, row['datetime'].tz_localize("UTC")) - 1)),
                axis=1)

            # Update the max tile id
            max_tile_id = max_tile_id * len(datetime_ranges)
            print(f'New MAX tile: {max_tile_id}')

        # Scale ids
        tile_ids = st_tdf['tile_ID']
        tile_ids.drop_duplicates(inplace=True)

        new_tile_ids = (tile_ids - 0) / (max_tile_id - 0)

        mapping = pd.Series(new_tile_ids.tolist(), index=tile_ids.tolist()).to_dict()
        st_tdf['tile_ID'] = st_tdf['tile_ID'].map(mapping)

        # Compute tiles sequences
        logging.info("Computing tile sequences")
        sequences = {}

        for index, l in st_tdf.iterrows():
            try:
                if l['tile_ID'] not in sequences[l['tid']]:
                    sequences[l['tid']].append(l['tile_ID'])
            except KeyError:
                sequences[l['tid']] = [l['tile_ID']]

        # Padding

        # max length
        max_length = 0
        for i in sequences.keys():
            if len(sequences[i]) > max_length:
                max_length = len(sequences[i])

        for i in sequences.keys():
            sequences[i] = [0] * (max_length - len(sequences[i])) + sequences[i]

        return sequences
