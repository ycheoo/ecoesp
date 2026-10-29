"""Override of the contributed `googleapiclient.model` PyInstaller hook.

The stock hook packs every discovery document the library ships — one JSON
per Google API, ~95MB. This app talks to Gmail only, so this override (same
file name, found first via the spec's hookspath) ships just that document.
The metadata copy is kept from the stock hook: googleapiclient.model queries
its own version through package metadata at import time.
"""

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

datas = copy_metadata('google_api_python_client')
gmail = collect_data_files(
    'googleapiclient.discovery_cache',
    includes=['documents/gmail.v*.json'])
# collect_data_files returns nothing, without a word, when the pattern
# matches no file, as it would if a library update moved or renamed
# the documents. The binary would then build and pass every check
# before a release, and fail at its first real run, where
# build('gmail', 'v1') finds no document: the release job's
# empty-config run stops before that call.
if not gmail:
    raise SystemExit('hook-googleapiclient.model: no Gmail discovery '
                     'document in googleapiclient.discovery_cache')
datas += gmail
