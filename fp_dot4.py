# Dot product 4 elementi — tipico in inferenza neurale con NVFP4
# [a0,a1,a2,a3] · [b0,b1,b2,b3]
# Input: a=a0, b=a1, c=a2, d=a3 (primo vettore)
#        le costanti simulano il secondo vettore [1,2,3,4]
w0 = 1
w1 = 2
w2 = 3
w3 = 4
p0 = a * w0
p1 = b * w1
p2 = c * w2
p3 = d * w3
s01 = p0 + p1
s23 = p2 + p3
result = s01 + s23
return result
