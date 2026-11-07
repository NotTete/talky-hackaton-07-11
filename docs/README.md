# Talky × Anthropic — Hackathon de conciliación bancaria a escala

**Grupo Valtermia** (ficticio) es un grupo industrial español del tamaño de una empresa del IBEX-35: diseña, fabrica y
distribuye equipos, recambios, química y servicios para instalaciones de agua (piscina, wellness, riego). 17 sociedades en
15 países (una más en FINAL), 12 divisas, 13 bancos, ~50 cuentas, decenas de miles de clientes —desde cadenas que pagan cientos de facturas
con una sola transferencia hasta instaladores que pagan por recibo domiciliado— y un departamento de tesorería que cada
mañana recibe cientos de miles de movimientos bancarios al semestre.

**Tu reto:** para cada movimiento bancario, decir qué partidas abiertas (facturas, abonos) liquida, por qué importe, y
—si no liquida ninguna— qué es (comisión, nómina, barrido de cash pooling, impuesto…), marcando las excepciones
(pagos parciales, descuentos por pronto pago, gastos bancarios, retenciones, diferencias de cambio, devoluciones,
duplicados, cobros no identificados…).

Todo es **sintético**: ninguna empresa, banco, persona, NIF o IBAN es real (los IBAN tienen dígitos de control válidos pero
códigos de entidad inventados). La única excepción son los tipos de cambio de `masters/fx_rates.csv`: son los tipos de referencia reales del BCE (el dírham marroquí, que el BCE no publica, se deriva de la cesta 60 % EUR / 40 % USD de Bank Al-Maghrib). Los volúmenes, formatos y patrones están calibrados con datos públicos de un grupo real y
con patrones abstraídos de extractos reales.

## Fases

| Fase | Fechas | Qué recibes |
|---|---|---|
| TRAIN | 2021-07-01 → 2025-06-30 (4 años) | datos **y** etiquetas (`train/golden_allocations.csv.gz`) |
| LIVE | 2025-07-01 → 2025-12-31 | datos desde el inicio; ranking en vivo |
| FINAL | 2026-01-01 → 2026-06-30 | datos al final del evento; **un único envío puntuado** por equipo; ranking final |

Hay *drift* a propósito: un banco migra de Norma 43 a camt.053 en octubre de 2025, un gran cliente cambia el formato de
sus avisos de pago, en FINAL se adquiere una filial nueva (Sudáfrica, ZAR), clientes que se renombran, cambian de banco o
pagan más tarde.

## Qué hay que entregar

Un CSV por fase con una fila por asignación — ver **FORMAT.md**:

```
bank_txn_id,item_id,amount,category,exception
BT-2025-0001234,ARI-2025-0456789,1210.00,,
BT-2025-0001240,,,BANK_FEES,
```

Puntuación = 100 × (0,5 · F1 de asignaciones + 0,3 · exactitud por movimiento + 0,2 · macro-F1 de excepciones).

- **LIVE**: envía tantas veces como permitan los límites; el ranking en vivo muestra tu puntuación.
- **FINAL**: **un único envío puntuado por equipo**: sin reenvíos y sin elegir entre varios ficheros. Un fichero rechazado
  por la validación no cuenta (corrígelo y vuelve a subirlo). La puntuación queda oculta hasta la revelación de las 20:30;
  el ranking final es esa única puntuación de FINAL y los empates se resuelven a favor del envío más temprano. Valida antes
  tu fichero con LIVE/TRAIN.

## Por dónde empezar

1. Lee **POLICIES.md**: son las reglas con las que se ha etiquetado (tolerancias, descuentos, cuándo es `PARTIAL`,
   diferencias de cambio, devoluciones, qué es `UNIDENTIFIED`…). Las etiquetas las siguen al pie de la letra.
2. **DATA_DICTIONARY.md** describe cada fichero y columna.
3. `sample/` contiene un mes de TRAIN con sus etiquetas para probar rápido.
4. `starter/starter.py` carga los datos, hace un emparejamiento ingenuo (importe exacto + número de factura en el texto)
   y escribe una entrega válida de LIVE. `starter/bankrec_score.py` es el mismo evaluador del servidor: puedes puntuarte
   en TRAIN.

```
python starter/starter.py --phase live --out mi_entrega_live.csv.gz
python starter/score_train.py mi_entrega_train.csv.gz      # puntuación sobre TRAIN (ver starter/README)
```

## Por qué es difícil (y por qué la escala importa)

- El número de factura casi nunca aparece tal cual en el banco: el nombre del pagador (truncado) es la clave principal;
  las referencias vienen sin prefijo, con dígitos cambiados, cortadas a 16/35/38 caracteres o con el pedido del cliente.
- Un cobro puede pagar 300 facturas (con aviso de pago que llega días después), una factura puede pagarse en tres plazos,
  y una remesa SEPA de 400 recibos llega como un único apunte.
- Mucho contexto clave sólo está en PDFs ("pago 2 de 3", "anula y sustituye a…", "retención de garantía del 5 %",
  "a deducir anticipo…", "servicios de mayo").
- Divisas: el banco convierte con su propio cambio; las comisiones internacionales llegan en un apunte aparte.
- No todo cobro tiene factura: algunos deben marcarse como no identificados en vez de forzar un emparejamiento.
- Medio año LIVE son ~210.000 movimientos y ~750.000 asignaciones sobre casi un millón de partidas: hace falta automatizar.

¡Suerte!
