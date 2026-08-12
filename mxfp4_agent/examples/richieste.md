# Richieste di esempio

Da usare così:

```bash
python run.py -f examples/richieste.md   # (usa solo la prima sezione: copiane una)
python run.py "<incolla qui la richiesta>"
```

## 1. Dot-product combinatorio (il caso base)

> Unità di dot-product MXFP4 puramente combinatoria su blocchi da 32 elementi
> E2M1 con scala condivisa E8M0. L'accumulo deve essere esatto (aritmetica
> intera in unità di 1/4) e il modulo deve esporre separatamente l'accumulatore
> intero e l'esponente condiviso risultante, più un flag di NaN sulla scala.

## 2. MAC pipelined con uscita FP32

> Unità MAC MXFP4 pipelined a 2 stadi: primo stadio decodifica e moltiplica i
> 32 elementi, secondo stadio somma con albero bilanciato e normalizza il
> risultato in FP32 (IEEE-754 binary32, round-to-nearest-even). Ingressi con
> handshake valid/ready semplificato (solo valid in ingresso e valid in uscita
> ritardato di 2 cicli).

## 3. Moltiplicatore element-wise

> Moltiplicatore element-wise MXFP4: dati due blocchi da 32 elementi produce 32
> prodotti in formato FP32. Combinatorio. Gestire esplicitamente lo zero
> negativo e il subnormale 0.5.

## 4. Convertitore MXFP4 → BF16

> Unità di conversione da un blocco MXFP4 (32 elementi + scala E8M0) a 32 valori
> bfloat16. Poiché la scala è una potenza di due, la conversione deve essere una
> pura ricomposizione di campi: nessun moltiplicatore. Gestire la saturazione
> dell'esponente BF16 e il caso scala = 0xFF (NaN).

## 5. Quantizzatore FP32 → MXFP4

> Unità di quantizzazione da 32 valori FP32 a un blocco MXFP4: calcola amax,
> deriva la scala condivisa E8M0 come floor(log2(amax)) - 2 + 127, poi codifica
> ogni elemento in E2M1 con round-to-nearest-even e saturazione a ±6.
> Pipelined a 3 stadi.

## 6. Accumulatore multi-blocco

> Accumulatore MXFP4 che somma i dot-product di N blocchi consecutivi in un
> accumulatore FP32, con interfaccia streaming (valid/last) e reset
> dell'accumulo su `start`.

## 7. Variante SystemVerilog

> Come la richiesta 1, ma in SystemVerilog puro (`--target systemverilog`),
> pensata per essere integrata in un pipeline esistente senza toolchain Scala.
