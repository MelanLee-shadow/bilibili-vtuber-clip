"""Synthetic dated grants for offline quota tests, never deployment authority."""

def synthetic_quota_authority():
    authority = 'Synthetic fixture for offline parser and reservation tests only.'
    return {
        'schema_version': 'lidousha-talk-quota-policy-authority.v1', 'authority': authority,
        'default_policy': {'cap': 5, 'extra_slot_min_score': None, 'authority': authority,
                           'song': {'cap': 1, 'extra_slot_min_score': None, 'authority': authority}},
        'entries': [
            {'entry_id': 'synthetic-game', 'recording_date': '2026-08-07', 'scope': 'game',
             'cap': 10, 'extra_slot_min_score': 85, 'authority': authority},
            {'entry_id': 'synthetic-event', 'recording_date': '2026-08-08', 'scope': 'event',
             'cap': 15, 'extra_slot_min_score': 85, 'authority': authority},
            {'entry_id': 'synthetic-song', 'recording_date': '2026-10-21', 'scope': 'song',
             'cap': 2, 'extra_slot_min_score': None, 'authority': authority},
        ],
    }
