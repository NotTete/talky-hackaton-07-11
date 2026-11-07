# TREASURY brief (TRAIN golden via tmp/pq; scripts tmp/t1-t18.py)
0. SAMPLE WARNING: sample has only 9 accounts (ES01x5, FR01x3, DE01x1): NO ES00, no payment factory, no pool header legs, no FX deals, ~no IC. Sample score says nothing for this domain -> measure on TRAIN by segment.

1. Treasury categories by TEXT REGEX (counterparty_account is NULL on all of them -> own-IBAN logic never fires; why sample IC/INTERNAL = 0.0, pool .49). One classifier: 19,244/19,250 correct, 0 FP on TRAIN (6 IC missed, truncated CHAPS/SPEI).
 - CASH_POOL (12,576): `CASH CONCENTRATION|ZBA SWEEP TO HEADER|TRASPASO CASH POOLING|TRASPASO ENTRE CUENTAS ES00|TRASPASO PROPIO`. Header leg ES00-PENA-EUR / ES00-RHEI-EUR "CASH CONCENTRATION ES02 9690" (participant entity + last4). Participants ES01/ES02/PT01-PENA, FR01/DE01/NL01/IT01-RHEI. Trap: "TRASPASO ENTRE CUENTAS ES00" is CASH_POOL not INTERNAL.
 - INTERNAL_TRANSFER (1,936): `TRASPASO (A|DE) CTA \d{4}` (last4 of other own account, unique across 47 accounts, same entity 1,936/1,936).
 - FX_DEAL (1,551): `FX DEAL \d{10} (CCY/CCY)` or `(COMPRA|VENTA) DIVISA USD 1.000.000 OP 2506235`; N43 code 13 / MT940 NFEX / camt FORX/SPOT; mostly ES00.
 - INTERCOMPANY (3,187): `FINANCIACION IC VALTERMIA`, `TRANSF. IC VALTERMIA`, `PRESTAMO IC|PRESTAMO INTRAGRUPO VALTERMIA`, `DEVOLUCION PRESTAMO (IC )?VALTERMIA`; rail prefixes (BONIFICO A FAVORE DI CAUSALE, SPEI ENVIADO CONCEPTO, PIX ENVIADO, PRZELEW WYCH. TYTUL:, CHAPS, TT IN, VIR SEPA RECU /MOTIF); truncated `CHAPS  FINANCIACION IC VA`, `TRANSF. IC VALTERM`. Trap: bare PRESTAMO = LOAN_REPAYMENT (344). No party named Valtermia.
 - Bare POOL trap: on pool accounts 6,780/8,369 "POOL" lines are customer receipts ("POOL SUPPLY…"). Never POOL/SWEEP/TRASPASO/PRESTAMO alone.
 - Leg pairing (same day, opposite amount, same ccy): CASH_POOL 100%, INTERNAL 100%, IC 66%, FX never. Tie-break only.
 - LIVE: CASH_POOL 1,568, INTERNAL 238, IC 372, FX 212.

2. AR payer != item party. Golden never puts an AR item on another entity than the account (0).
 a) ar_third_party_parent (1,298 lines, 7.9 rows/line, 183 ARC, no exceptions): payer = item party's parent_customer_id (1,157; 138 unrecognisable). "POR CTA DE <child>" = on behalf of (~150 lines). Rule: candidates = payer ∪ children(payer). Do NOT widen to group_id (sibling ~absent). 6 parent ids not in customers.csv.
 b) ar_third_party_confirming (4,998, 1 row): "ABONO CONFIRMING NORTE PAGOS CONFIRMIN | G EFC | ORD. OQD DEPURACION S.L.U. FRA FV24-02 | 04580 VTO 060424"; FATIR lines 1,982. Party = name after ORD.; glue doc across " | " ("FV24-02 | 04580" -> FV24-0204580); VTO ddmmyy == due_date.
 c) ar_third_party_agency (2,636 lines; has SD 202, PARTIAL 128, SW 37, OVERPAYMENT 10, SBC 9): agencies ARVENTO RECOVERIES LTD, RECOBROS ÁGORA S.L., GESTIÓN DE IMPAGADOS DELTACOBRO; `EXPTE <n> <DEBTOR truncated> <doc suffix>` in 2,622/2,636 ("EXPTE 65190 DAVID REED 0108915" -> INV25-0108915). EXPTE also in 17 duplicate lines.
 d) N43 `| NNNNNNNN-NAME` 8-digit ordering id: 55,896 lines, 3,111 ids, 3,104 map to one customer; LIVE 93% of ids known from TRAIN. Build alias tables from TRAIN golden.
 e) trade_name: 3,275 customers; bank text uses trade name 15,995 vs legal 16,155 lines -> index both. "ACH CREDIT AQUATICS PORT WI…" = Lake Harmon Leisure Corp.
 f) Names not keys: within an entity 7,029 names shared by 23,393 customers. Name -> SET.
 g) blocked_since does NOT filter (24,255 rows paid after block).

3. IBAN changes (ar_new_iban 1,122 lines): all customers with NOTICE_<party>_<date>.pdf. TRAIN masters hold NEW iban 603/633; LIVE masters hold OLD iban 76/79 (21 LIVE lines pay from new iban not in masters). 74% of lines have no IBAN, often no ref -> name+amount. Index = masters ∪ notice new ∪ notice old. Trap: IBAN regex catches SEPA creditor ids `CRED+US00ZZZ146127579` (2.6k LIVE) -> exclude ..00ZZZ. Customer IBANs unique.

4. AP: cross-entity only ES00 account paying ES01/ES02/PT01 items (42,390 lines; ap_paid_by_holding 33,363). AP scope: ES00 account -> {ES00,ES01,ES02,PT01}; else own entity.
 - Payment runs for ES00-paid items are filed under the ITEM entity (payment_runs/ES01_*.csv with debtor_account_id=ES00-PENA-EUR). Index runs by debtor_account_id across all files. "PAGO TRANSFERENCIAS | 4799-56-2805375-66 792280 000228": 2,000/2,000 matched on (debtor account, document_number, transfer_amount). Also "TRANSFERENCIA A <SUPPLIER truncated>". 164 supplier names collide across entities -> use beneficiary_iban.
 - ap_factored (10,934 lines, 26,777 rows, 494 PARTIAL, 567 APC, 1 supplier/line): we pay the FACTOR: "BONIFICO A FAVORE DI CREDIFÁCIL COMERCIAL EFC CAUSALE FRAS 22002025 22002030 22002055", "PIX ENVIADO RIBAGORZA CONFIRMING FRA 23003932", "ACH DEBIT NORTE PAGOS CONF VENDOR PMT". Factors (not in suppliers): Confirming Peñalara, Credifácil Comercial EFC, Ribagorza Confirming, Norte Pagos Confirming EFC. 37,812 run rows have factor as beneficiary -> party-free doc resolution in entity scope + sum must match cash; PDF assignment clause corroborates.
 - ap_confirming (6,387 lines, 9.6 rows/line): "PAGO CONFIRMING | REMESA CF010024092529 9 PROVEEDORES"; run_type confirming by run_id CF…; debit can be months later (other phase folder).

5. Masters traps: IT01-VALD-EUR-2 NOT in bank_accounts.csv but 10,529 TRAIN + 1,689 LIVE lines -> entity from line column. ZA01-UMVC-ZAR already in LIVE (2 lines "EFT CREDIT POOL & SPA TAYLOR LTD PROFORMA PF25-0100001") -> CUSTOMER_ADVANCE. Load masters per phase (FINAL masters_final). Last-4 account refs unique.

6. RULES: R-T1 treasury regexes before item search (19,250 lines, 99.97% R, 0 FP). R-T2 never bare POOL/SWEEP/TRASPASO/PRESTAMO. R-T3 AP entity scope + runs by debtor_account_id. R-T4 factor counterparty -> party-free doc resolution (VERIFY acc). R-T5 confirming receipt party after ORD., glue doc, VTO. R-T6 agency EXPTE -> debtor name + suffix. R-T7 parent payer ∪ children; POR CTA DE. R-T8 alias tables (8-digit id, names, trade_names) from TRAIN; name -> SET. R-T9 IBAN index masters ∪ notices; exclude ZZZ. R-T10 no blocked filter. R-T11 trust line entity; ZA01 proforma -> CUSTOMER_ADVANCE. R-T12 leg pairing as tie-break only.
