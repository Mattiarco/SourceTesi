def accelerator(a, b):

    def mul_add(x, y):
        return x * y + y

    c = mul_add(a, b)
    d = mul_add(c, a)

    total = 0

    for i in range(3):
        for j in range(2):
            total = total + (i * j)

    if total > c:
        total = total - c
    else:
        total = total + d

    out = total * d
    return out