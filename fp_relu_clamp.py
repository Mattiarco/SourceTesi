# ReLU con clamp — usato nell'inferenza con formati FP4
# ReLU: max(0, x), poi clamp al valore massimo rappresentabile MXFP4 (6.0)
# Input: a = valore da attivare
zero = 0
max_val = 6
neg = a < zero
result = a * neg  # azzera se negativo (PHI node nel DFG)
over = result > max_val
clamped = result - over  # riduce se oltre il range FP4
return clamped
