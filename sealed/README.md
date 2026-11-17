# sealed/

Holds `payload.tar.seal`, the encrypted harness payload, once you seal it:

```sh
SEAL_KEY=... python3 ../seal/seal.py pack \
  --in .. --out payload.tar.seal
```

`../run_containerized.sh` unseals it into a throwaway directory before
`docker build` and wipes it on exit. If this directory has no blob, the
build context stays the harness directory itself, exactly as before, so
unsealed setups are unaffected.

Never commit the key here — only the blob. See `../seal/SEALED.md` for
the threat model, rotation, and what sealing does not protect.
