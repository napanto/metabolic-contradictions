# Addendum 08C: choosing the reasoning effort of the corpus classifier

Registered on Monday 16 November 2026 at 02:56 CET, by Antonio's decision: «Prova anche ragionamento max per classificazione GLM sul sample studenti. Quando hai i risultati, decidi se usare ragionamento basso, alto o max […] sul corpus intero sulla base del giudizio di astra e fable».

The rule below is fixed before the max-effort run exists and before any system label is compared with an oracle or a student answer. It extends section 6 of amendment 08 and section 3 of addendum 08A.

## Candidates

GLM-5.3 Flash classifies the 35 live pairs from their corrected (v2.1) claims, with the frozen classification prompt and the registered settings, at three reasoning efforts:
- **low:** the system as registered;
- **high:** already run;
- **max:** run after this registration, with the same output cap as high (131072). One run per pair, with the registered single identical retry.

## Reference and measure

- **Reference.** The oracle reference of amendment 08 section 5, with validity under addendum 08B: the category on which Astra and Fable agree. A pair without such a reference is left out.
- **Primary measure.** The number of reference pairs where the setting's binary flag equals the reference flag. A pair is flagged when its category is contradictory, apparent contradiction or not replicated. An invalid system label counts as a mismatch.
- **Secondary measure.** The number of reference pairs whose category matches. It is reported but does not decide.

The student answers play no part in the choice. Collection is still open, and the student comparison remains an independent check, reported for every setting.

## Rule

Order the candidates by cost: low, then high, then max. Choose the lowest-effort candidate whose primary measure is at most one pair below the best one. A higher effort is therefore chosen only if it beats every cheaper setting by at least two reference pairs. With 35 pairs, a one-pair difference is noise.

## Consequences

- **Corpus classification.** The corpus classification of addendum 08A (11,500 candidate pairs, roughly) uses the chosen effort. It is applied before classification starts, and everything else stays unchanged. If the chosen effort is not low, the corpus is classified by a revised classifier, and the report says so. The student validation of the system's label (low) does not change.
- **Optimism.** For the chosen setting, agreement with the oracles on these same 35 pairs is optimistic, because those pairs selected it. The report states this. Its agreement with the students is not affected by that selection.
- **Funding.** If the projected cost at the chosen effort exceeds the fresh balance, the run waits for Antonio's top-up. The effort is never lowered to save money.
