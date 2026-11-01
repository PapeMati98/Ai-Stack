# Source-preserving publication

Normal contributors never publish stable wiki. An explicit curator acts on the
configured owner machine under the existing atomic curator lock. Curation authority
does not authorize application deployment, IAM, messages or secret operations.

1. Freeze the admitted backlog's source/proposal IDs and exact original hashes.
   Keep new arrivals as a separately measured delta.
2. Produce project syntheses with dated claims, exact source locations, conflicts,
   unresolved questions and complete linked evidence histories. Preserve originals.
3. Assign every frozen item a disposition. Historical/unresolved/orphan means it is
   accounted for, not accepted as a current fact. Repetition is not corroboration.
4. Run `scripts/curation_check.py --candidate <dir>` FIRST. It settles deterministically
   every quantity, date and "there is none" claim on the page against the manifest,
   the ledger and the frozen inputs. A finding here must be fixed before any model is
   asked to read the candidate. Measured 2026-09-21: six independent review rounds on
   one candidate found ten defects, eight of them arithmetic or date claims, at a cost
   of roughly 15.7 million contractor tokens. The checker settles those in a second and
   leaves the review for the part only judgement can do.
   THEN obtain a real independent content review bound to the exact candidate publication
   manifest and source hashes. Hashes alone do not prove faithful meaning. A changed
   candidate or source invalidates the review.
5. Use `scripts/curation.py` to validate and publish the reviewed manifest under the
   retained owner lock. The transaction owns only allowlisted wiki outputs, generated
   indexes, its curation ledger and event. All source/proposal inputs stay read-only.
6. On failure restore only this transaction's unchanged output bytes/preimages. If a
   third party changed a target, stop recovery without overwriting their work. Keep
   the journal for diagnosis. Release only the lock ID acquired for this operation.
7. Verify lint, source-byte reconciliation, fresh-consumer recall and the published
   revision. Report semantic review, live verification and unresolved backlog
   separately. A successful lint or a larger Obsidian graph is not semantic acceptance.

The publisher is not a scheduler or a general shell/Obsidian command interface.
Claw Ops can submit normalized admitted inputs and observe results; it must not
become a second writer of stable wiki or substitute an Office message for authority.
