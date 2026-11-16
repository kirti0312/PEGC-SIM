"""Exact Python port of src/engine/rng.js (mulberry32 + deriveSeed).

Using the identical generator and draw order means the SUMO side receives the same
vehicles (times, origins, destinations, routes, speed factors) as the browser engine.
"""
M = 0xFFFFFFFF


class Rng:
    def __init__(self, seed: int):
        self.a = (seed & M) or 1

    def next(self) -> float:
        self.a = (self.a + 0x6D2B79F5) & M
        a = self.a
        t = ((a ^ (a >> 15)) * (1 | a)) & M
        t = (((t + (((t ^ (t >> 7)) * (61 | t)) & M)) & M) ^ t) & M
        return ((t ^ (t >> 14)) & M) / 4294967296

    def range(self, lo: float, hi: float) -> float:
        return lo + (hi - lo) * self.next()

    def int(self, n: int) -> int:
        return int(self.next() * n)

    def pick(self, arr):
        return arr[int(self.next() * len(arr))]


def derive_seed(seed: int, salt: int) -> int:
    h = ((seed & M) ^ (((salt + 1) * 0x9E3779B1) & M)) & M
    h = ((h ^ (h >> 16)) * 0x85EBCA6B) & M
    h = ((h ^ (h >> 13)) * 0xC2B2AE35) & M
    return (h ^ (h >> 16)) & M
