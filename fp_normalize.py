# Normalizzazione con scala e shift — comune nel preprocessing MXFP4
# output = (a - mean) / scale
# Con mean=2, scale=4 (costanti simulate)
mean = 2
scale = 4
shifted = a - mean
result = shifted / scale
return result
