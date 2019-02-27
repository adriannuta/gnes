import os
from typing import List

import GPUtil
import faiss
import numpy as np
import tensorflow as tf

from . import BaseEncoder as BE

DEVICE_ID_LIST = GPUtil.getAvailable(order='random',
                                     maxMemory=0.1,
                                     maxLoad=0.1,
                                     limit=1)
if DEVICE_ID_LIST:
    os.environ['CUDA_VISIBLE_DEVICES'] = str(DEVICE_ID_LIST[0])


def train_kmeans(x: np.ndarray, num_clusters: int, num_iter: int = 20) -> np.ndarray:
    kmeans = faiss.Kmeans(x.shape[1], num_clusters, num_iter, True)
    kmeans.train(x)
    return kmeans.centroids


class PQEncoder(BE):
    def __init__(self, k: int, m: int, num_clusters: int = 50, model_path=None):
        super().__init__(model_path)
        self.k = k
        self.m = m
        self.num_bytes = int(k / m)
        self.num_clusters = num_clusters
        self.centroids, self._centroids_expand = None, None
        self._build_graph()
        self._sess = tf.Session()
        self._sess.run(tf.global_variables_initializer())

    def _build_graph(self):
        self.ph_centroids = tf.placeholder(tf.float32,
                                           [self.num_bytes,
                                            self.num_clusters,
                                            self.m])
        self.ph_x = tf.placeholder(tf.float32, [None, self.num_bytes, self.m])
        # [self.num_bytes, None, self.m]
        self.x = tf.transpose(self.ph_x, [1, 0, 2])
        ty = tf.reduce_sum(tf.square(self.ph_centroids), axis=2, keepdims=True)
        ty = tf.transpose(ty, [0, 2, 1])
        tx = tf.reduce_sum(tf.square(self.x), axis=2, keepdims=True)
        diff = tf.matmul(self.x, tf.transpose(self.ph_centroids, [0, 2, 1]))
        diff = tx + ty - 2 * diff
        # start from 1
        self.p = tf.argmax(-diff, axis=2) + 1
        self.p = tf.transpose(self.p, [1, 0])

    @BE.as_train_func
    def train(self, vecs: np.ndarray):
        assert vecs.shape[1] == self.k, 'Incorrect dimension for input!'
        res = []  # type: List[np.ndarray]
        for j in range(self.num_bytes):
            store = vecs[:, (self.m * j):(self.m * (j + 1))]
            store = np.array(store, dtype=np.float32)
            res.append(train_kmeans(store, num_clusters=self.num_clusters))

        self.centroids = np.array(res, dtype=np.float32)
        self._centroids_expand = np.expand_dims(self.centroids, 0)

    @BE.train_required
    def encode(self, vecs, batch_size=10000, use_gpu=True) -> bytes:
        if use_gpu:
            return self.encode_gpu(vecs, batch_size)
        else:
            return self.encode_cpu(vecs)

    @BE.train_required
    def encode_gpu(self, vecs, batch_size=10000) -> bytes:
        num_points = vecs.shape[0]
        vecs = np.reshape(vecs, [num_points, self.num_bytes, self.m])
        i = 0
        res = []
        while batch_size * i < vecs.shape[0]:
            m = batch_size * i
            n = batch_size * (i + 1)
            tmp = self._sess.run(self.p,
                                 feed_dict={self.ph_x: vecs[m:n],
                                            self.ph_centroids: self.centroids})
            res.append(tmp)
            i += 1
        return np.concatenate(res, 0).astype(np.uint8).tobytes()

    @BE.train_required
    def encode_cpu(self, vecs: np.ndarray) -> bytes:
        x = np.reshape(vecs, [vecs.shape[0], self.num_bytes, 1, self.m])
        x = np.sum(np.square(x - self._centroids_expand), -1)
        x = np.argmax(-x, 2) + 1

        return np.array(x, dtype=np.uint8).tobytes()