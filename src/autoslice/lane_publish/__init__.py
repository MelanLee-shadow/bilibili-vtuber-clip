"""lane_publish: the upload tool of step 10 written for the public tree, from step 10's interface.

Step 10 (manual_lane/step10_*) runs an upload tool on the media host by subcommand and reads what it writes.  This package is
that tool, built from that interface alone: every subcommand step 10 sends, the files it reads back, and the receipts the lane's
receipt chain (manual_lane/receipt_chain) and the publication registry readers (manual_lane/title_history) walk.

    audit <package> --json               the sealed package is intact (audit.py)
    make-manifest ... --out M             what is to go online, every file bound by sha256 (manifest.py)
    verify --manifest M                   the files are still those bytes
    upload --manifest M                   new BV: biliup, collection, public check, registry row (first.py)
    repair-plan / cover-repair-plan       same BV: what replaces what, against what is live now (repair.py)
    repair-status / cover-repair-status   0 done, 6 not yet
    repair-run / cover-repair-run         the replacement on Bilibili; again after a stop resumes, never repeats
    repair-verify-live / cover-...        read the live BV back, write the completion receipt, move the registry row
    metadata-plan / -run / -status /      same BV, only its title, tags and description, by its registry row alone (no
      -verify-live                        package, video, cover or recording is read; metadata.py)

Return codes are step 10's: 0 done; 2 refused before anything (input, configuration); 3 refused by a gate (registry, ledger,
manifest); 4 the shared lock is held by another upload or repair (`LOCK_BUSY` in the words); 5 Bilibili or biliup failed;
6 not done yet; 7 posted but not public within the wait (run `upload` again later: it resumes, it does not post twice);
8 quota (the account's rolling 24-hour submission window is full).

Where things are (`common.Config`): the runtime base is AUTOSLICE_BASE (step 10 sets it) or the profile layout's
`autoslice_base`; under it `state/publication_registry.runtime.v1.json` (the channel's one registry overlay, see registry.py),
`state/lane_publish_ledger.jsonl` (every
dispatch, so nothing is posted twice), `state/lane_publish.lock`, and `lane_publish.json` (this host's biliup and cookie file).
The waits and the publish policy are lane_rules.json `upload.publish`.  No account, collection or cookie is in the code: the
collections are the channel profile's `season_routes` asset."""
