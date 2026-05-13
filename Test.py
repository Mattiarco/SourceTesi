def accelerator(a, b):

    c = a + b

    d = c * b

    if d > 20:
        d = d - a

    for i in range(4):
        d = d + i

    e = d * c

    return e