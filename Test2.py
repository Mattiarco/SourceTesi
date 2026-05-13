def accelerator(a, b, c):

    x = a * b
    y = x + c

    if y > 10:
        y = y - b
    else:
        y = y + c

    i = 0
    while i < 3:
        y = y + i
        x = x + y
        i = i + 1

    z = x + y
    return z