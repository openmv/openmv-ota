# The OTA server Schemathesis fuzzes in CI (the `fuzz-api` job): the server tests' own
# construction -- SQLite, local disk, a verifier that registers every camera -- with Live and
# the datalake configured (grants are signed here, nothing is called), an all-scopes admin
# token, and a few cameras written the way a check-in writes them so list and detail routes
# have rows. Serve with: uvicorn --app-dir fuzz api_app:app --port 8765
import tempfile

from openmv_ota.server.app import create_app
from openmv_ota.server.auth import hash_token
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.scopes import ALL_SCOPES
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from openmv_ota.server.verify import Registration

TOKEN = "fuzz-admin-token"


class _Verifier:
    def verify(self, board, device_id):
        return Registration(True)


tmp = tempfile.mkdtemp()
store = SqliteMetadataStore(tmp + "/ota.db")
store.migrate()
store.add_token(hash_token(TOKEN), "fuzz", list(ALL_SCOPES))
for i in range(3):
    store.upsert_device(device_id="OPENMV_N6:%024d" % i, product_id=7, board="OPENMV_N6",
                        current_version="1.0.0", current_payload_version=0x01000000,
                        confirmed=1, account_id="")
app = create_app(
    ServerSettings(base_url="http://127.0.0.1:8765", swd_ids_verify_url="http://127.0.0.1:9/",
                   swd_ids_verify_token="t", capability_secret="c" * 32,
                   live_relay_url="https://live.invalid", live_token_secret="l" * 32,
                   datalake_url="https://data.invalid", datalake_token_secret="d" * 32),
    metastore=store, storage=LocalArtifactStorage(tmp + "/blobs"), verifier=_Verifier())
