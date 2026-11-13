# Security policy

OPxFlasher writes to phone firmware, so safety bugs matter as much as security bugs. Please
report privately:

- a way to make OPxFlasher write to a partition it should refuse (identity partitions, personal
  data, the wrong phone or slot), or skip one of its checks;
- a way for a ROM, backup, profile, debloat list or logs zip to run commands or code;
- a problem with the release builds or their signatures.

**How to report:** use [Report a vulnerability](https://github.com/ox1d3x3/OPxFlasher/security/advisories/new)
(GitHub private advisory). Please don't open a public issue for these. You'll get an answer as
soon as possible, and credit in the release notes if you'd like it.

Only the latest release candidate or release is supported with fixes.

## Antivirus false positives

Windows Defender occasionally flags the single-file `.exe` (a known issue with PyInstaller
apps). Verify the file as below, then report it to Microsoft as incorrectly detected at
https://www.microsoft.com/en-us/wdsi/filesubmission, and let us know in an issue.

## Verifying releases

Release files are signed with GitHub build attestations:
`gh attestation verify <file> --repo ox1d3x3/OPxFlasher`. See the
[README](README.md#verify-your-download).
