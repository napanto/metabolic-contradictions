# Addendum 08B: validity of oracle answers rests on their coded answers

Registered on Monday 16 November 2026 at 02:54 CET, by Antonio's decision ("Ok, registrala e applicala", about 02:53 CET). It amends section 3 of `PROTOCOL_AMENDMENT_08_TWO_JUDGES.md`. It is a declared deviation, taken after the oracle run and before any comparison. To take it, nobody compared an oracle answer with a student answer, with the other judge, or with the system's label. The only facts used were the validator's error messages and a re-validation that reports valid or invalid.

## What happened

The amendment-08 run ended at 02:38 CET:
- **Astra:** 35 of 35 answers valid.
- **Fable:** 25 of 35 valid. The other 10 pairs failed both attempts.

Every failure concerns the free-text field "Briefly describe the difference" and none concerns a coded answer:

| Error | Attempts |
|---|---|
| Text in a field where the page shows no box ("Explanation without an other difference", "Unexpected outcome detail") | 11 |
| Text longer than the 200 characters the box accepts | 9 |

On the site the text box appears only after the participant picks "Another difference", and it accepts at most 200 characters, so a student cannot produce either error. The oracle receives the whole form as one JSON schema in which the field always exists. Its prompt said only "briefly describe" and "set inapplicable details to null". It never stated the 200-character limit.

## Rule

1. **Validity rests on the coded answers.** For both judges, an answer is valid when its coded answers pass the site's validation rules unchanged. The coded answers are the Yes/No/Cannot-tell choices, problem codes, relevance, reasons and factors. The category and the factor derive from them alone, exactly as for a student. The free text never enters that derivation.
2. **Free text is kept as returned.** Where the page has no box, or the text exceeds 200 characters, the text is kept in the record exactly as the oracle wrote it. It does not make the answer invalid. Only for running the validator, the text is set to empty where the page has no box and cut at 200 characters where it has one. The stored answer and the derived category are not changed.
3. **The saved responses are re-validated; nothing is resent.** No new request is made. The first attempt that is valid under this rule is the item's final answer. The original results, invalid statuses and raw outputs remain in the archive unchanged.
4. **Sensitivity analysis.** The strict result under amendment 08 as first registered is reported next to the primary one: Fable 25/35, with the oracle reference and agreement measures restricted accordingly.

## Effect

The offline re-validation, run before this registration, finds all 10 affected Fable pairs valid at their first attempt, which gives Fable 35/35 under this rule. Astra's answers are unaffected.

Everything else in amendment 08 is unchanged.

## Correction (03:02 CET, before any comparison)

The sentence "Astra's answers are unaffected" was inaccurate. Five items succeeded only at their second attempt, after a first attempt that failed on free text alone: one for Astra and four for Fable. Under rule 3 the first attempt valid under this rule is final, so for these five items the first attempt replaces the second. The rule is applied as written. The second attempts stay in the archive and are recorded as `literal_rule_attempt` alternatives. The choice was made from validator messages only, without looking at either attempt's answers.
