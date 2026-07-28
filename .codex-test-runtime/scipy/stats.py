import math


class _NormalDistribution:
    @staticmethod
    def pdf(value):
        number = float(value)
        return math.exp(-0.5 * number * number) / math.sqrt(2.0 * math.pi)

    @staticmethod
    def cdf(value):
        return 0.5 * (1.0 + math.erf(float(value) / math.sqrt(2.0)))


norm = _NormalDistribution()
