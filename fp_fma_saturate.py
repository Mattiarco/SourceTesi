# Fused Multiply-Add con saturazione — unità FP avanzata per NVFP4
# Combina MAC + clamp in un unico datapath senza arrotondamenti intermedi
# FMA: (a * b) + (c * d), saturato a max_val=6

max_val = 6
zero = 0

# Due prodotti paralleli
p0 = a * b
p1 = c * d

# Somma parziale
fma = p0 + p1

# Saturazione inferiore (clamp a 0)
below = fma < zero
fma_pos = fma - below

# Saturazione superiore (clamp a max_val)
above = fma_pos > max_val
result = fma_pos - above

return result
