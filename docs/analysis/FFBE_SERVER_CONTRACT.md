# FFBE Server Contract — the client's server-facing surface (v9.0.0)

**Source:** `[FFBEWW]` — `libgame.so` (`/home/thetb/projects/ffbe/libgame.so`, ARM64, v9.0.0) and the
APK data dump (`/home/thetb/projects/ffbe/com.square_enix.android_googleplay.FFBEWW/`); `[Community]`
for the BraveHaxvius-era protocol notes in `/home/thetb/projects/ffbe/docs/analysis/COMMUNITY_RESEARCH.md` §4.
**Date:** 2026-09-13
**Status:** INVENTORY — area checklist + endpoint list recovered; request/response **schemas partially
recovered** (2026-09-13: community token→name lexicons pulled in — 93.5% of the 2017 request-field
and 95.9% of the response-object tokens are still valid at v9.0.0 — see
`docs/analysis/community_lexicons/README.md`).
**Scope:** what the *client* asks for and must be served. The server implementation is not present in any
artifact we hold. This doc is the server-side axis that `docs/COVERAGE_LEDGER.md` does not carry.
**Purpose (2026-09-13):** *not* a commercial server deployment. The target is a **single-player
reconstruction** — server-derived state and logic get folded into the game itself (story, party, units,
cutscenes first); community/social features (raids, friends, PvP, seasons) are deferred. This inventory
therefore exists to answer one question: **what does the client expect that our data does not contain?**

**Confidence key:** **high** = read out of `libgame.so` by symbol/disassembly, or a file on disk ·
**medium** = inference from adjacent strings/names, one corroboration · **low** = name/analogy only.

---

## 0. Verdict in one paragraph

The client is a conventional HTTPS + JSON client with a **per-request obfuscated endpoint table** and
**per-request AES keys compiled into the binary**, talking to exactly **one game host**
(`v900-lapis.gumi.sg`) under the base path `/lapisProd/app/php/gme/`. There are **211 distinct
`/actionSymbol/<8-char token>.php` endpoint paths** in `libgame.so`; **222 `*Request` classes** resolve
to them (10 paths are shared). The community statement "no endpoint list exists" is **refuted for the
client side**: the paths are literal strings, and each request class's request-ID key, encode key and URL
were recovered by disassembling its `getRequestID()`, `getEncodeKey()` and `getUrl()` (all three are
3-instruction `adrp`/`add`/`ret` stubs). What remains unknown is the **payload contract** — the JSON
field names are themselves 8-char obfuscated tokens (`UserInfo`-side `variables.json` in the community
work maps the same *style* of token), so a synthetic server can be routed to but not yet answered
correctly without recovering the field map.

---

## 1. Transport, host and route (area 11)

**Confirmed** — the community's transport claim (HTTPS · AES-ECB · Base64 · JSON) is correct in
structure and is now sourced to specific addresses.

| Fact | Evidence (`[FFBEWW]`) | Confidence |
|---|---|---|
| Host is `v900-lapis.gumi.sg` | string at `0x2f4431b`; returned by `PHP_URL_DOMAIN()` @ `0x1c409f0` | high |
| Base path is `/lapisProd/app` | string at `0x2f4432e`; appended (14 chars) by `PHP_URL()` @ `0x1c40a00` | high |
| Scheme `https://` | string at `0x2f5937c`, prepended by `CommonUtils::getPhpUrl()` @ `0x1e315d0` | high |
| Path segment `php/gme` (7 chars) | string at `0x2f69690`, appended by `WrapAsyncHttpConnector::getFullpath(char const*, char const*)` @ `0x1eef030` | high |
| Per-request path `/actionSymbol/<token>.php` | 211 distinct literals; each `*Request::getUrl()` returns one | high |
| HTTP client | cocos2d-x `network::HttpClient` / `HttpURLConnection` (`nm -D`; `send`, `setRequestMethod`, `setVerifySSL`, `getSSLVerification`) | high |
| JSON body model | `RequestData`, `ResponseData::parseObject/parseArray/parseValue`, `JsonNode`/`JsonGroup`; strings `Parse Error.`, ` RESPONSE=`, ` ERROR=` adjacent to the `php/gme` literal | high |
| AES-ECB primitive | `Crypto::aesEcb(unsigned char const*, unsigned long, std::string&, unsigned char const*, Crypto::CryptMode)` and `Crypto::_aesEcb(...)` | high |
| Base64 armour | `CommonUtils::encodeCStringForBase64WithNewCrypto` / `decodeCStringForBase64WithNewCrypto(char const*, char const*, std::string&)` | high |
| Per-request key | `*Request::getEncodeKey()` — 224 implementations, each returning a distinct 8-char token | high |
| Per-request request-ID | `*Request::getRequestID()` — 224 implementations, each returning a distinct 8-char token | high |
| Session signal key (server-issued) | `SignalKeyResponse::readParam(...)`, `UserInfo::getSignalKey/setSignalKey`, `BaseRequest::createSignalKeyTag()`, `ModelChangeUserInfo::getSignalKey` | high |
| No TLS pinning found | **SUPERSEDED by §1f — read that section, not this row (kept for provenance).** The original reading was absence-of-evidence only: no pinning/certificate strings; `HttpClient::getSSLVerification`/`setSSLVerification` exist but the value passed was not resolved | **§1f: high** |

**Assembled request URL (medium-high):**
`https://v900-lapis.gumi.sg/lapisProd/app/` + `php/gme` + `/actionSymbol/<token>.php`
- `CommonUtils::getPhpUrl()` returns `https://` + `PHP_URL()` + `/`; `PHP_URL()` returns host + `/lapisProd/app`.
- `WrapAsyncHttpConnector::getFullpath()` appends the literal `php/gme`.
- The community independently records the shape `.../lapisProd/app/php/gme/actionSymbol/{url}.php`
  (`[Community]`, `docs/analysis/COMMUNITY_RESEARCH.md` §4) — the versions differ in host naming
  (`lapisv230.gumi.sg` there vs `v900-lapis.gumi.sg` here), so the *host* is version-specific and the
  *path* is stable.

**Key pool, measured.** `grep -cE '^[A-Za-z0-9]{8}$'` over the string table = **3712** (3693 distinct).
Cross-check: the community field-name tokens `9Tbns0eI` (→ `UserId`), `7UR4J2SE` (→ `Gil`) and
`4rC0aLkA` (→ `UserItemInfo`) are present in this binary, but **none** of them is any `getEncodeKey()` or
`getRequestID()` return value — so field-name obfuscation and per-request keys are **distinct pools** of
the same shape. The per-request 8-char key is an AES-128 key material: the project has already
established the 16-byte form (8 chars + 8 NUL) for the MST path in
`docs/analysis/LOCALIZED_TEXT_FORMAT.md` ("Key length | 16 bytes (8-char alphanumeric + 8 null bytes)");
whether the network path null-pads identically is **not verified**.

**Request envelope** built by `BaseRequest` (all symbols `[FFBEWW]`):
`createBody()`, `addGroup(char const*)` / `addGroup(JsonGroup*)`, `createLoginGroup(JsonNode*, bool, short)`,
`createUserInfoTag()`, `createSignalKeyTag()`, `createVersionTag()`, `createResourceVersionTag()`,
`getPlayerToken()`, `getPgsTokens(LoginService*, short)`, `addPgsLoginInfo(JsonNode*, int, short)`,
`addForceGuestLoginChecksum(JsonNode*, int)`, `getParentMstVersion()`, `getParentResourceVersion()`,
`getFriendLeaderUpdateInfo()`, `setAppVersion(JsonNode*)`, `getSendData()`, `isAsync()`.
So every request carries: a login group (or guest checksum), a user-info tag, a signal-key tag, an
app-version tag, an MST-version tag and a resource-version tag, on top of the action's own body.

**Third-party, NOT the game server (`[FFBEWW]`):** `/sbx2/sbx_file.php`, `/sbx2/sbx_session.php`,
`/sbx2/sbx_user.php` plus host `crisbx.jp` — these are **CRIWARE SBX** (`criSbx_*` symbols), the audio
middleware's own analytics/proxy service. Ads/analytics SDK endpoints (below) are likewise not the game
server.

---

### 1e. The request/response envelope — VERIFIED 2026-09-16 `[FFBEWW]`

This is the **spec**, kept here with the rest of the transport facts. (§6 item 2 is the *plan* item and now only
carries what remains.) Established by reading v9.0.0's own code and, for the Java half, decompiling the game's
`classes.dex`; independently re-derived by an audit agent the same day.

| Fact | Value | Evidence |
|---|---|---|
| Cipher / mode / padding | **AES-128-CBC, base64 outside — and the body is padded TWICE**: a manual PKCS7 layer *then* Java's `PKCS5Padding`, so the ciphertext carries **16 bytes more than a single-pad implementation produces** | jadx of the APK's `classes.dex`: `LapisJNI.NEW_CRYPTO_ALGORITHM = "AES/CBC/PKCS5Padding"`, `paddingWithPKCSX(str, 16)`, `cipher.init(1, secretKeySpec, ivParameterSpec)` · **and live 2026-09-17**: the client's own request decrypts to 515 bytes of JSON followed by **13 × 0x0D** (the manual pad, taking it to 528) and its JNI `Cipher.doFinal` reports `in=528 out=544`; decoding our reply it reports `in=704 out=700`. **A single-padded reply is rejected** (`parse()` → 0 → `CONNECT_ERROR`); the boot advances only once the server double-pads. Full bisect: `APK_emulator_work/docs/notes/connect_step_requirements.md` §7.1 |
| Key derivation | the per-request 8-char **`EncodeKey`**, UTF-8 bytes, **NUL-padded to 16** | `byte[16]` + `System.arraycopy(key.getBytes("UTF-8"), 0, bArr, 0, min(16, len))`; same form as the MST manifest keys (3,098 entries, all length 8) — **confirmed live**: the client's `Cipher.init` uses `34666f58567757640000000000000000` = `4foXVwWd` + 8 NULs for `GameSettingRequest` |
| IV | **fixed literal `dZMjkk8gFDzKHlsx`** (16 ASCII bytes), handed from native as the 3rd Java argument — and it is the **same IV the MST CBC generation uses** | `0x1f3cc04`–`0x1f3cc80`; `scripts/mst_decode.py:80` ("single hit in .rodata"); MST mode tally `mst_decoded/_DECODE_REPORT.tsv` = **157 CBC / 62 ECB**, so the *IV* is shared while the *mode* is per-table |
| Where it runs | **Java, via JNI** — `CommonUtils::encodeCStringForBase64WithNewCrypto` (`0x1e31f14` → body `0x1f3cba0`) bridges to `LapisJNI.encodeCStringForBase64WithNewCrypto(String,String,String)` | `libgame.so`'s statically linked OpenSSL AES has **no direct/PLT caller** among all 832,943 `bl` in `.text`; `Crypto::aesEcb` (`0x19b64dc`) likewise has 0 |
| Both directions | the **same key**, through the same virtual slot `[vptr+0x28]` — concrete override `MissionStartRequest::getEncodeKey` (`0x19a8f10`) | request `BaseRequest::getSendData` (`0x198f0a4`); response `GameResponseParser::parseResponseAsync` (`0x190f318`) — **confirmed live**: `parse` passes `[RequestData vptr+0x28]` as `parseTag`'s `subTag` (`0x190e6b4`→`x21`), and the client's own decrypt of our reply used that exact key **and the same IV** (`ACTUAL_IV=645a4d6a6b6b386746447a4b486c7378` = `dZMjkk8gFDzKHlsx`, read back from the live `Cipher` after `init`) |
| Envelope field names | the 10 tokens (`t7n6cVWf`, `qrVcDe48`, `TEAYk6R1`, `ytHoz4E2`, `z5hB3P01`, `QCcFB3h9`, `LhVz6aD2`, `bCf65yoK`, `Wk1v6upb`, `c1qYg84Q`) are **10/10 present** in v9 | `docs/notes/bravehaxvius_assessment.md` §8, re-measured independently |
| Client provenance | the APK in the repo is the same build: its `libgame.so` is **byte-identical** to ours (sha256 `a1276540…6937e4`) | audit 2026-09-16 |

**The correction this carries, because it is the kind of thing that silently corrupts a probe:** the 2017 clientless
client this lineage came from used **ECB**; v9 is **CBC**. Its **key form and vocabulary transferred, its mode did
not.** Residuals: `Crypto::aesEcb`'s dead-ness is proven only for direct/PLT calls (a GOT `ldr`+`blr` would evade
the scan), and the mode proof rests on the Java decompile rather than native code (no
`encodeCStringForBase64WithNewCrypto` exists in `classes2/3.dex`, so a second envelope implementation is unlikely).

### 1f. TLS trust and pinning — VERIFIED 2026-09-16 `[FFBEWW]`

**Verdict: the client does not pin, and it verifies with the platform default. A CA in the Android *system*
store is therefore sufficient** — a *user*-store install is not (`targetSdkVersion=33`), and a patched APK is
needed only if `/system` cannot be written. Full evidence, commands and limits: `APK_emulator_work/docs/notes/tls_pinning_findings.md`.

| Question | Answer | Evidence |
|---|---|---|
| Which layer opens the socket | **Java `HttpsURLConnection`** (the cocos2d-x SDK's `Cocos2dxHttpURLConnection`) — not native, despite the linked OpenSSL | `HttpClient::send` (`0x1eec970`) → `networkThread/Alone` → `processResponse` → `HttpURLConnection::init/sendRequest`; JNI strings `org/cocos2dx/lib/Cocos2dxHttpURLConnection` |
| Is verification on? | **yes, platform default** — not a hard-coded `NONE` | native `SSL_CTX_set_verify` has exactly **1** call site (`0x1ed6e60`), inside libcurl (off-path); the cocos path's `HttpURLConnection::setVerifySSL` (`0x2a2ac40`) early-returns on an empty string, and its only setter `HttpClient::setSSLVerification` (`0x2a28a6c`) has **0 call sites** |
| Pinning? | **none, either side** | `SSL_CTX_set_cert_verify_callback` 0 calls; `X509_VERIFY_PARAM_set1_host` appears only inside OpenSSL's `SSL_dane_enable`; **0 × `BEGIN CERTIFICATE`** in the binary; no `sha256/<pin>` literal in the APK (only the bundled okhttp `CertificatePinner` *class*); `network_security_config.xml` allows cleartext to `127.0.0.1` only, with no `trust-anchors`/`src="user"` |
| libcurl | **statically linked (7.52.1) but off the request path** | `strings -a libgame.so` → `libcurl/7.52.1`, `vtls/openssl.c`; hidden visibility, so `nm -D` structurally cannot see it — the trap is `RE_VERIFICATION_IDIOMS.md` **#18** |
| The `SSL_SRP*` symbols | **OpenSSL's own module, never handshaked — nothing to implement** | only `SSL_CTX_set_srp_username`/`_password` are called, both inside that same libcurl branch |

**Residual (medium, runtime-only):** an SDK global `Le/f/a/a/r;.fixHttpsURLConnection()`
(`setDefaultSSLSocketFactory`) exists but is not called anywhere in the reachable code scanned; if a runtime path
reaches it, a default socket factory could bypass verification. Static reading cannot settle this — only a live run.

**Consequence:** the acceptance test (the APK against our server in a rooted emulator) needs a **system-store CA
plus host redirection** — both standard on a rooted image. No pinning workaround, no patched APK.

## 2. Area checklist

### 2.1 Auth / login / session

**(a) What the client does.** Presents a login service (guest, Google Play Games, Facebook, Apple,
Google), obtains/refreshes a token, connects, fetches or creates the account (`CreateUserRequest` /
`GetUserInfoRequest`), and can transfer an account by transfer-code. A feature-switch request runs at
login.

**(b) Proving symbols (`[FFBEWW]`, `nm -D`).**
`LoginService::login/relogin/onAPI/processLoginCallback/getAccessToken/getRefreshToken/getUserID/isTokenExpired`,
`LoginService::GoogleLoginService/FacebookLoginService/AppleLoginService/PlayGamesLoginService/DefaultLoginService`,
`LoginServiceManager::{login,tryRelogin,canRelogin,checkLoginValidity,getCurrentLoginService,saveLoginInfo,clearLoginInfo,onServiceAPI,onServiceLogin}`,
`LoginScene::{initConnect,requestConnect,checkConnectResult,checkUpdateDeviceID,isNeedUpdateSwitch,requestUpdateSwitch,initPurchase}`,
`ConnectScene::{initConnect,loadFiles}`, `ConnectRequestList::{addObject,getRequest<T>,getObjectByRequestID}`,
`UserInfo::{getUserID,getPassword,getAccountID,getAccountType,getGauthID,getGauthSecret,getGumiLiveID,getGumiLiveToken,getPgsID,getUserGoogleID,getUserFacebookID,getUserAppleID,getContactID,getUserDeviceID,isGuestAccount,getNewDeviceFlg,getSkipUpdateDeviceId}`,
`SignalKeyResponse::readParam`, `UpdateSwitchInfoRequest::createUpdateSwitchTag`, `UpdateDeviceIdRequest`,
`TransferCodeIssueRequest`, `TransferCodeCheckRequest`, `ModelChangeUserInfo::getPassword/getSignalKey/getSqmkCsID`.

**(c) Data shape.** Login-response user record recovered as getters on `ModelChangeUserInfo` /
`UserInfo` (names are the real field semantics): `accountID`, `userID`, `oldUserID`, `password`,
`signalKey`, `sqmkCsID`, `handleName`, `friendID`, `contactID`, `accountType`, `deviceTransferEnable`,
`tutoCheckFlg`, `actionPoint(+Max/ReceiveTime/RestTime)`, `clsmOrbPoint(+Max/ReceiveTime/RestTime)`,
`friendPoint`, `gil`, `exp`, `lv`, `giftId`, `modelChangeCnt(+Pc/Sp)`, capacity adds
(`unitCntAdd`,`itemCntAdd`,`equipItemCntAdd`,`materiaCntAdd`,`materialItemCntAdd`,`friendCntAdd`), and
purchase fields (`purchaseFixFlg`,`purchaseModelStatus`,`purchaseReceipt`,`purchaseRstFlg`,
`purchaseSignature`,`purchaseTransactionID`). The wire encoding of these is **not** recovered.

**Lead — NOT established: the persisted credential.** The same data dir's `shared_prefs/auth.xml` holds
`name = TAMcdDi6c7vK` and
`password = Hipnosis,0,1,0,0,0,sWCFGy6MZeeb,0,0,0,0,0,0,0,0,0,0,0`. The first field of `password` matches
`gamedata.xml`'s `keynew_handlename = Hipnosis`, which is suggestive — but **ownership of that file is not
established**: `libgame.so`'s only `auth` literal is an HTTP-digest token (`qop="`, `md5-sess`, `auth`,
`auth-int`, `auth-conf`), not a prefs filename, and the dex's `auth` strings sit among SDK auth constants.
Settle it by finding the writer (which component calls `getSharedPreferences("auth", …)`) before treating
these as the client's `UserInfo::getPassword()` / `name` values. Source: a real v9.0.0 session's data dir
(dated 2024-10-26), not a spec.

**(d) Synthetic server must supply.** A login/connect endpoint set that (i) accepts the login group +
guest checksum, (ii) returns a user record and a **signal key**, (iii) answers `CreateUser` for a fresh
account, (iv) answers `GetUserInfo{,2,3}` / `UpdateUserInfo`, (v) answers `UpdateSwitchInfo` (feature
flags) and `UpdateDeviceId`, and (vi) answers `InitializeRequest`. Held client versions to expect:
`keyparent_mst_version = 5465`, `keyparent_resource_version = 0`, `keyCONFIG_VERSION_KEY = 660`
(`/home/thetb/projects/ffbe/com.square_enix.android_googleplay.FFBEWW/shared_prefs/gamedata.xml`).
Confidence **high** on the symbols, **low** on the payload shapes.

### 2.2 Resource delivery (CDN)

**(a) What the client does.** Downloads master-data tables ("MST") and resource CPK packs, and
localized-text tables, then keeps them on disk; it decides what to fetch by comparing its held
`parent_mst_version` / `parent_resource_version` against server-supplied version tables.

**(b) Proving symbols / strings (`[FFBEWW]`).**
`DownloadSequencePopup::{requestStartupMst,requestMstDownload,requestResDownload,requestFullDownload,requestResources,requestMapResources,requestTownResources,requestGachaResource,requestLocalizedTextMstDownload,addResourceMapSize,downloadSizePrompt}`,
`InitialDownloadScene::initResourcesCpk`, `CommonUtils::{getResourceUrl,needsDownload}`,
`AllResourceVersionMstResponse`, `ResourceVersionMstLocalize{,Base,List,Response}`,
`ResourceMapVersionMstLocalize*`, `MapResourceVersionResponse`, `ResourceMapSizeMstList/Response`,
`sgSplitLocalizedTextMstList`, `LoginScene::MIN_MST_FILE`, `checkResourceVersion`, `saveResourceVersion`,
`setUpResourceVersion`, `removePreviousResourceVersions`.
Strings: format `Ver%d_%s`; regex `^Ver[0-9]+_[A-Za-z0-9]+\.dat$`; `resources.cpk`,
`resources_common.cpk`, `resources_tutorial.cpk`, `resources_pretutorial.cpk`, `character.cpk`;
config keys `P_GS_CDN_PATH`, `F_URL_MST`, `P_GS_APP_ENV`.

**(c) Data shape — the on-disk contract (directly observed).**
`/home/thetb/projects/ffbe/com.square_enix.android_googleplay.FFBEWW/files/`:
- `mst/` — **219 `.dat`** files named `Ver<N>_<8-char token>.dat` (e.g. `Ver325_0EvyjKh8.dat`,
  `Ver100_V6Qke9wf.dat`) plus **47 `<token>.i`** index files; all 47 `.i` tokens have a matching `.dat`
  (measured with `comm -12`). `<N>` is a per-table data version, not the app version (range observed
  **`Ver2`…`Ver735`** — re-measured 2026-09-13 with `ls *.dat | sed -E 's/^Ver([0-9]+)_.*/\1/' | sort -n`
  → min 2, max 735, 219 files; the earlier `Ver10`…`Ver325` reading was partial and is superseded).
  Note the 219 *on-disk* tables are a different number from the 217 `keyF_*_MST` preference keys.
- `localized_texts/en/` — **145 `.dat`**, same `Ver<N>_<token>.dat` naming.
- `en/sd/*` and `common_lang/{sd,sound}` — extracted resource-pack trees.
Version negotiation values: `keyparent_mst_version=5465`, `keyparent_resource_version=0`,
`keymst_downloaded_flg=valid`, `keymap_res_ver_5000002`, `keyF_RESOURCE_VERSION_MST_LOCALIZE`,
`keyF_RESOURCE_MAP_VERSION_MST_LOCALIZE` (`gamedata.xml`).

**The version manifest row's field set — measured 2026-09-18.** `VersionInfoResponse::readParam` (the
`c1qYg84Q` group) reads **four** tokens per row: `a4hXTIm0` (name), `wM9AfX6I` (version), **`dT2gJ1vH`
(file size)** and **`D0f6Cn7Q` (string — the filename token)**. A synthetic reply that sends only the first
two still works (the client builds `Ver<N>_<token>` itself), so the omissions are a **lead, not a proven
cause** — and adding rows must be done carefully, because injecting an invented record into this reply
previously made `InitializeRequest` retry. Note also that **no server field selects the MST decrypt method**:
the loader hardcodes CBC-first with a silent ECB fallback (`libgame.so` `0x1d54d74`, `w5=1`), so a
`BadPaddingException` burst on an ECB-stored table is expected noise, not a data failure. Evidence:
`/home/thetb/projects/ffbe/APK_emulator_work/docs/notes/mst_crypto_dispatch.md`,
`.../notes/base64_decode_path.md`, `.../notes/resource_download.md`.
- `homeMarquee/` — **8 downloaded banners + `marquee_banner.ver`**, and that `.ver` file is plaintext
  `id,version,url,localpath` rows (8 of them) whose URLs are all
  `https://d3syu63yncawjw.cloudfront.net/lapis-static-prod/dlc_assets_prod/en/sd/static_banner/marquee/<name>.png`.
  **So the real asset host is a CloudFront distribution, `d3syu63yncawjw.cloudfront.net`, with the path prefix
  `lapis-static-prod/dlc_assets_prod/` and the locale + `sd` tier inside it** — the concrete value the server
  steers via `P_GS_CDN_PATH` (§2.2(d)(ii)), **observed, not inferred** (`high`). It also shows the client
  caches a fetched asset as `id,version,url,localpath`, i.e. the refresh contract for a server-listed asset.

**(d) Synthetic server must supply.** (i) The version tables (`F_RESOURCE_VERSION_MST*`,
`F_RESOURCE_MAP_VERSION_MST*`, `AllResourceVersionMst`) telling the client which `Ver<N>_<token>.dat`
is current per table; (ii) a CDN base URL — resolved through the server-pushed URL dictionary
(`EnvUrlMst`/`P_GS_CDN_PATH`), see §2.8; (iii) the `.dat` payloads **verbatim** (the encoded
base64+AES-128 files we already hold for 219 tables — `docs/analysis/LOCALIZED_TEXT_FORMAT.md`) so the
client does not need to re-derive them. Note `CommonUtils::getResourceUrl()` resolves through config,
not a hardcoded host (only `Sbo89` and `/` appear as literals in it), so the CDN host is **server-configurable**.

### 2.3 Player save / instance state

**(a) What the client does.** Holds the player's authoritative state in memory (`UserInfo`,
`UserTeamInfo`, `UserDiamondInfo`, `UserUnitInfo`, `UserParty`, `UserItemInfo`, `UserEquipItemInfo`,
`UserMateriaInfo`, `UserVisionCardInfo`, …) and fills it from server responses; it holds **no local
serialization of that model** — but it does keep its *in-progress* state (see (c4)).
*settings* and *version/identity* locally, not the roster.

**(b) Proving symbols (`[FFBEWW]`).** The `User*Info` singleton family — `UserInfo` (117 `nm -D` lines),
`UserTeamInfo` (76), `UserVisionCardInfo` (30), `UserEquipItemInfo` (23), `UserQuestInfo` (17),
`UserDiamondInfo` (14), `UserMateriaInfo` (9), `UserItemInfo` (6) — plus `UserUnitInfo` (55) and
`UserParty` (0 defined symbols at v9.0.0; party data lives behind `UserPartyDeckList` /
`BasePartyInfoList`) — line counts include vtables/thunks, not distinct methods. Responses:
`UserUnitInfoResponse`, `UserItemInfoResponse`, `UserEquipItemInfoResponse`, `UserMateriaInfoResponse`,
`UserVisionCardInfoResponse`, `UserTeamInfoResponse`, `UserActualInfoResponse`, `UserPartyInfo*`,
`UserGiftInfoResponse`, `UserImportantItemInfoResponse`, `UserCarryItemInfoResponse`;
`UserInfoResponse::readParam(...)`. 496 `readParam` symbols exist in total.

> **Correction 2026-09-16 (AX-46).** This section previously cited `ss::Player` ("4074 symbol lines") and
> `ss::UserData` (1160) as evidence of player state. **Both are false positives** — `ss` is SpriteStudio:
> `ss::Player` is the SS5 **animation player** we ported for AX-26 (`getMaxFrame`, `play`, `setFrameNo`,
> `animePause`, `updateFrame`) and `ss::UserData` is its per-part user-data for `setUserDataCallback`. The
> namespace collided with "player" and the counts made it look like the biggest class in the model. Removed
> above; the `User*Info` family is the whole player state.

**(c) Data shape.** Per-response parsers are `*Response::readParam(int, int, char const*, char const*, bool)`
— 496 `readParam` symbols in `nm -D`. The individual field names inside the player-state payloads were
**not** decoded when this section was first written; they **were** later recovered, and (c1)–(c3) below are
that recovery.
`gamedata.xml` carries **411 keys** (`grep -o 'name="[^"]*"' ... | wc -l`), of which **364 are
`keyF_*`**, **217 end in `_MST`** and **145 are `keyF_TEXT_*`**; the rest are local
settings/versions/identity — **and, as (c4) records, a set of in-progress player keys that this sentence
wrongly denied**. The "no unit/party/inventory data" reading was refuted 2026-09-16 by audit; the master-vs-instance
boundary recorded in `docs/analysis/FFBE_UNIT_DATA_MODEL.md` §7 still holds for the *roster*.

**(c1) The player-state field map (recovered 2026-09-16, `[FFBEWW]`).** Each row is read from the binary by
disassembling the accessor pair (`nm -DC --defined-only libgame.so | grep 'UserTeamInfo::'`, then the 8-byte
`get`/`set` bodies) — offsets are hex, and the width is the load/store width.

| class | offset | width | field | getter |
|---|---|---|---|---|
| `UserTeamInfo` | +0x24 | int32 | rank / level | `getLv` |
| | +0x28 | int32 | exp (progress into the rank) | `getExp` |
| | +0x30 | **int64** | gil | `getGil` |
| | +0x38 | int32 | friend point | `getFriendPoint` |
| | +0x3c | int32 | action point (**NRG now**) | `getActionPoint` |
| | +0x40 | int32 | action point max (**NRG cap**) | `getActionPointMax` |
| | +0x44 | int32 | NRG rest time | `getActionRestTime` |
| | +0x48 | int64 | NRG receive time (epoch) | `getActionReceiveTime` |
| | +0x58…+0x74 | int32 ×8 | the `<X>CntAdd` capacity adders (unit, storage, friend, equip item, item, materia, material item, vision card) | `getUnitCntAdd` … |
| | +0x90 | int32 | gift id | `getGiftId` |
| | +0x98 | std::string | profile message | `getMessage` |
| | +0xb0 | int32 | consecutive-login count | `getSerialLoginCount` |
| | +0xb4 | int32 | player emblem id | `getPlayerEmblemId` |
| | +0xb8 | std::string | home decoration vision card | `getDecorateHomeVcId` |
| `UserDiamondInfo` | +0x24 / +0x28 | uint32 ×2 | the two lapis balances | `getPaidDiamond` / `getFreeDiamond` |
| | — | derived | **`getTotalDiamond()` is the two summed** (`0x1da9f78`: `ldp w8,w9,[x0,#0x24]; add w0,w9,w8`) — the total is not stored | `getTotalDiamond` |

**`actionPointMax` has no clamp in its own setter, but the client does bound what you can HOLD.**
`UserTeamInfo::setActionPointMax` (`0x1d87fe8`) is a bare `str w1,[x0,#0x40]` — no clamp, no lookup — so the
online *cap* is whatever the server sent. **Corrected 2026-09-16 on audit:** the claim "nothing anywhere bounds
it" is wrong — `UserTeamInfo::incActionPoint` (`0x1d87a00`) reads the define key
**`ENERGY_POSSESSION_LIMIT`** (= **`9999`**) and stores `min(actionPoint + delta, 9999)` into `+0x3c`, so a
player can *hold* up to 9999 NRG — four digits, well above the level cap. `updateActionTime` additionally stops
generating NRG once `actionPoint >= actionPointMax`. And there is no literal NRG-cap *constant* in the binary
(the audit's sweep); the per-rank value is table data instead (240 at rank 300, 265 at row 350, per (c3)),
while the client's only read of that table is the rank-up **delta** (`getUpUserActionPoint`), not a clamp. So a
host can *compute* a plausible `actionPointMax` offline from rank (queued as AX-48) while the server remains its
authority online (wire key `m0bD2zwU`) — and must honour the possession limit when granting NRG.

**(c2) The server's wire keys (recovered 2026-09-16, `[FFBEWW]`).** The response parsers compare the incoming
field name against 8-character obfuscated keys; each `strcmp` is followed by a branch to that key's store, so the
key ↔ field map is recoverable by reading the branch targets. Two parsers carry the whole player state:

`UserTeamInfoResponse::readParam` (`0x19226c8`, singleton in `x19`, values via `atoi` — except the TFA orb,
`CommonUtils::StrToInt`):

| wire key | → field (offset) | | wire key | → field (offset) |
|---|---|---|---|---|
| `7wV3QZ80` | Lv (+0x24) | | `7UR4J2SE` | gil (+0x30, `sxtw` ⇒ 64-bit) |
| `B6H34Mea` | Exp (+0x28) | | `0XUs3Tv6` | friendPoint (+0x38) |
| `B6kyCQ9M` | actionPoint (+0x3c) | | `Utx8P1Qo` | giftId (+0x90) |
| `m0bD2zwU` | actionPointMax (+0x40) | | `Wk1v6upb` | message (+0x98, string) |
| `5sEbN01y` | actionRestTime (+0x44) | | `gXb32xzQ` | decorateHomeVcId (+0xb8, string) |
| `T1Lm7WdD` | unitCntAdd (+0x58) | | `i6qUd7bG` | serialLoginCount (+0xb0) |
| `NULFd7v3` | storageCntAdd (+0x5c) | | `n69quiCJ` | playerEmblemId (+0xb4) |
| `tsT4vP7w` | friendCntAdd (+0x60) | | `xDm19iGS` / `zo76TkaG` / `T1J8Avne` | clsmOrb point / max / restTime (+0x50 object) |
| `Xu7L1dNM` | equipItemCntAdd (+0x64) | | `Ddsg1be4` / `Le8Z5cS0` / `zR0pYjb2` | eventOrb point / max / restTime (+0x78) |
| `4BI0WKjQ` | itemCntAdd (+0x68) | | `9vBE36PW` / `n3G5ItVX` / `UIL6Ft1T` | equipGrowOrb +0x80, same three |
| `92LoiU76` | materiaCntAdd (+0x6c) | | `rq9wWrt0` / `K3NhECDq` / `gEyJKQBa` | tfaEventOrb +0x88, same three |
| `9w3Pi6XJ` | materialItemCntAdd (+0x70) | | | |

`UserDiamondInfoResponse::readParam` (`0x1921bcc`): **`T7sah9rc` → paid (+0x24)**, **`cb3WmiD0` → free (+0x28)**. (So
"free" is the field at +0x28 — worth stating because `getTotalDiamond` sums them and the order is not obvious.)
Neither `UserInfoResponse` nor `UserActualInfoResponse` touches the diamond members.

The orb objects are `UserPointBase` (`+8` point, `+0xc` max, `+0x10` restTime, `+0x18` receiveTime) and each
restTime key additionally raises `NotificationManager::setLocalNotificationFlg(true)` — the client arming the
local "energy is full" notification. `UserTeamInfoResponse::onReadFinished` (`0x1922d1c`) is **4 bytes**: a tail
`b` to `LocalNotificationManager::sendEnergyNotification()`; it mutates nothing.

**Two gaps worth knowing.** `+0x74` (`visionCardCntAdd`) has **no wire key** in either parser — the server never
fills it, so a model that carries it is carrying a field the client's own reader leaves at whatever the ctor set.
And the parser's entry gate is `ResponseData::isAsync == 0` (`ldrb w8,[x0,#0x22]`) with further `[x22,#0x22]`
guards on the stamina keys, so the apply order is not unconditionally "every field every time".
Derivation, per-key branch targets and the negative results: `axmol_work/docs/notes/ax46_player_blob.md`.

**(c3) The rank table, and the two stamina rules (derived from shipped data, 2026-09-16).** `UserLevelMstList` is
`F_TEAM_LV_MST` (350 rows; `mst_decoded/F_TEAM_LV_MST.ndjson` — regenerable). The client indexes it by level
(`UserLevelMstList::getObjectWithLv`), and `GameUtils::getNeedUserExp(lv)` (`0x19e8d98`) is that row's `+0x28`.

| rule | where | value | verification |
|---|---|---|---|
| Energy regeneration | define key **`RECOVER_TIME_ACTION`** → `DefineMst+0x54` (`applyVarivable` `0x1d2422c`) → `DefineMst::getRecoverTimeAction()` `0x1d245d4` = `ldr w0,[x0,#0x54]` | **`300`** (seconds) | `F_DEFINE_MST.ndjson` row `RECOVER_TIME_ACTION`, JP label 行動力回復時間 "action point recovery time"; consumed against `time(NULL)` in `UserTeamInfo::updateActionTime()` (`0x1d87a64`), so the unit is seconds — **+1 NRG per 300 s** |
| Max NRG per rank | `F_TEAM_LV_MST` column `B6kyCQ9M` — *the same token the response uses for `actionPoint`*, and **proved** to be the client's own field: `UserLevelMstResponse::readParam` (`0x1927528`) `strcmp`s that token then `str w0,[x22,#0x2c]`, which is `UserLevelMst+0x2c` (= `setActionPoint` `0x1c41468`) | `lv + 40` for `lv ≤ 100`; `140 + ⌊(lv−100)/2⌋` above | **0 mismatches over all 350 rows.** lv 1 = 41, lv 100 = 140, lv 300 = **240** |
| The client's own use of it | `GameUtils::getUpUserActionPoint` (`0x19ed568`) = `row(lv)`\[+0x2c\] − `row(lv−1)`\[+0x2c\] — the NRG a rank-up grants, read from the same field | — | disassembly |
| Its rows are the raw shipped table | `Ver50_NCPTw2p0.dat` — 350 lines, independently decrypted (base64 → AES-128-CBC) and compared object-by-object with the ndjson | — | **0 mismatches, 350/350 rows**; `gamedata.xml` selects token v50 (`keyF_TEAM_LV_MST` = `50`) (audit 2026-09-16) |
| Client's rank bound | `UserLevelMstList::getMaxTeamLv()` `0x1c41674` | **350** — it returns the **last row's** `+0x24` (`elements[size-1]`) | disassembly above |
| Where progression stops | `F_TEAM_LV_MST` need-exp column `B6H34Mea` | **frozen at `5445433` from lv 300 through 350** | 51 identical values; the NRG column keeps rising (265 at 350), so rows 301–350 are generated but unreachable |

So the community figure "rank cap 300, max energy 240" splits three ways — and **the third part was refuted
2026-09-16 on audit.** (i) **240 at rank 300 is exact**, from the table. (ii) The table runs to 350 rows, and the
client's own `getMaxTeamLv()` (`0x1c41674`) returns the last row's `+0x24` = 350. (iii) ~~"300 is where the exp
requirement stops advancing, so 301–350 are unreachable"~~ — **that inference was backwards, and the measurements
it rested on survive while the inference does not.** The *cumulative* requirement is frozen at `5445433` from 300 to
350, so the **delta is 0** for every step 301→350: those ranks cost **no additional exp at all**, i.e. they are
*trivially reachable*, not blocked. And the operative cap is a **define-table value**: the level-up loop reads
`DefineMst::getIntValue("MAX_TEAM_LV")` (row `MAX_TEAM_LV` = **`350`**, JP label 最大部隊レベル) and bails at
`cmp w22,w0; b.ge` (~`0x2451514`–`0x2451524`), then compares total exp against `GameUtils::getNeedUserExp(lv)`.
**Nothing in the client enforces 300**, and no `300` immediate was found in the level-up code. So if a 300 cap is
real it is **server-side**, while the client's data would allow 350 and **265 NRG** there. Lesson recorded as
`RE_VERIFICATION_IDIOMS.md` A17: a frozen threshold makes a step *free*, not *forbidden* — test the direction of an
inference, not just its arithmetic.

And the level cap is **not** the most NRG a player can hold: `incActionPoint` (`0x1d87a00`) clamps *holding*
at the define key `ENERGY_POSSESSION_LIMIT` = **9999** (see the note under (c1)) — so the header's stamina label
must fit a 4-digit value against a 3-digit cap, which is why the client's content-dependent size rule exists at
all: it has exactly **two reachable outputs, 16 and 15** (`home_screen.md` §2.2b).

**Token identity — measured, and it corroborates the master/instance boundary.** The 8-char obfuscated tokens are
**field identities from one shared dictionary**, used both as MST column names and as response wire keys. Measured
over the decoded MST set: the response's `7wV3QZ80` (Lv) is a column in **12** tables (team level, unit grow, exploit…
beast, vision-card, limit-burst level tables), `B6kyCQ9M` (actionPoint) in `F_MISSION_MST` (a mission's NRG *cost* —
its values are small multiples of 5, max 100) and `F_TEAM_LV_MST`, `B6H34Mea` (Exp) in 3 tables, `7UR4J2SE` (gil) in
7 cost/reward tables. Conversely the fields that are **instance-only have zero column hits**: `m0bD2zwU`
(actionPointMax), `T1Lm7WdD` (unitCntAdd), `0XUs3Tv6` (friendPoint), `n69quiCJ` (playerEmblemId) appear in **no**
master table. That is the master-vs-instance split showing up independently, from the data side, in the same
place the payload analysis put it.

**There IS a name source, and there was all along (corrected 2026-09-16 on audit).** This section originally
described the tokens as undecoded and set confidence to *medium* on that basis. Both were stale: the repo has held
**`docs/analysis/community_lexicons/variables.json`** since 2026-09-13 — a community-derived **token → name
lexicon** with **371 entries / 368 distinct names** (its own README records that 347/371 tokens are still present
verbatim in v9.0.0). The lexicon *family* is wider still: the union of its four files is **564** 8-char tokens
(`variables.json` 371, `response.json` 195, `files.json`/`network.json` covering other key shapes — and each
file carries a documented parse quirk, e.g. a JS `//` comment and a trailing comma, so both need a pre-pass before
`json.loads`). Checked against the decoded set: **143 of 904 MST column tokens are directly named by the lexicon
(15 %), and only 3 of 219 tables are *fully* named by it** (182 partly, 34 not at all).

**But that is a FLOOR, not a ceiling — and calling it a ceiling was this section's own error (corrected the same
day).** The lexicon is the *weakest* of three naming routes; the strong one is the **game's own vocabulary**:

| route | what it gives | measured |
|---|---|---|
| **the row struct's accessors** | a human name per field (`UserLevelMst::setActionPoint`, `setUnitCnt`, `setColosseumOrb`, …) — the game's *own* names, not a community label | **1,857 named `*Mst::set*` accessors across 240 `*Mst` classes** |
| **the per-table parser** | the token → struct-offset map | **215 `*MstResponse::readParam`** implementations — ≈ one per decoded table (219) |
| the community lexicon | a name per token, where it has one | 143 of 904 column tokens |

**Worked example, and the reason for the correction:** `F_TEAM_LV_MST` has **23 columns** and `UserLevelMst` has
**21 named setters** — so **21 of 23 columns are nameable from the binary alone**, where the lexicon names only three
of that table's tokens. Join token → offset (from the parser) → accessor name (from the struct) and a token grid
becomes a named schema. That join is **mechanical** — the same two primitives `tools/disasm_annotate.py` already
resolves — i.e. the schema pass is a *script*, not a research project; and it is *validated* by the worked example
(the rank table's expected 21-name set is assertable). What remains genuinely consumer-driven is **meaning,
priority and verification** (the `unbounded` half) — a name is not a validated semantic. The scale of the pass:
215 parsers × their `strcmp`→store pairs, joined to 1,857 accessor offsets. "143/904" should never again be read
as "85 % unknowable". All eight tokens probed here resolve in it —

| token | lexicon name | token | lexicon name |
|---|---|---|---|
| `7wV3QZ80` | **Level** | `m0bD2zwU` | **EnergyMax** |
| `B6kyCQ9M` | **Energy** | `T1Lm7WdD` | **ExtraUnits** |
| `B6H34Mea` | **Experience** | `0XUs3Tv6` | **FriendPoints** |
| `7UR4J2SE` | **Gil** | `n69quiCJ` | *(absent — its `playerEmblemId` label comes from the accessor at `+0xb4`, which stands on its own)* |

That lexicon **independently corroborates** the getter-derived field names in (c1) (Energy ≈ `actionPoint`,
Level ≈ `Lv`, Experience ≈ `Exp`, Gil ≈ `gil`), and it raises the dictionary reading's confidence: ~one name per
token (371 → 368, only 3 names having >1 token), with no counterexample among the most value-divergent mapped
tokens. **Residual, and stated rather than implied:** 761/904 column tokens stay unnamed, so "one dictionary" is
supported over the named 143, not proven over the whole space.

**And the tokens are provably in the shipped bytes, not an artifact of our decode.** The strongest attack on this
claim was circularity — *did the decoder get the tokens from the client's parsers?* No: the MST `.dat` files are
base64 lines whose plaintext is the JSON itself, and the decoder (`scripts/mst_decode.py`) derives only the AES
key from a `.rodata` manifest and then `json.loads` the decrypted text — no token→name table is consulted:

```
head -1 cpk_extract/**/mst/Ver50_NCPTw2p0.dat | base64 -d > /tmp/line1.bin
openssl enc -d -aes-128-cbc -K 71503754475a45380000000000000000 \
    -iv 645a4d6a6b6b386746447a4b486c737800 -nopad -in /tmp/line1.bin
  -> {"7wV3QZ80":"1","B6H34Mea":"0","B6kyCQ9M":"41","Sp8s3BoH":"1000", ...}
```

So the raw bytes are reachable and the chain is closed at ground truth. **Use this recipe rather than arguing about
whether a decoded artifact reflects the game** (`docs/analysis/RE_VERIFICATION_IDIOMS.md` A3).

**One logical fact, two client-side copies — and the class is `ModelChangeUserInfo`, NOT `UserInfo`.**
**Corrected 2026-09-16 on audit.** The login/model-change record carries a PARALLEL set of the progress fields at
`+0x14c` Lv, `+0x150` Exp, **`+0x154` Gil (`int`, 32-bit)**, `+0x158` FriendPoint, `+0x15c` ActionPoint,
`+0x160` ActionPointMax, `+0x168` ActionReceiveTime (`str x1`, 64-bit) — while **`UserInfo` has none of them**
(a `nm` check: every `getLv`/`setGil`/… symbol belongs to `ModelChangeUserInfo`, 12 of 12). The runtime model's
`gil` is a `long long` at `UserTeamInfo+0x30`, so the width mismatch on `gil` is real. The UI reads the
`UserTeamInfo` copy; a host model that keeps one home per fact should follow `UserTeamInfo` and say so.
*(How this was got wrong: an earlier `sed 's/.*UserInfo:://'` swallowed the `ModelChangeUserInfo::` prefix and
made that class's accessors look like `UserInfo`'s. Do not trust a greedily-stripped class name.)*

**(c4) What the client DOES keep locally — a correction (recovered 2026-09-16).** This section previously
asserted that the only local persistence is settings/version/identity, and cited the key-count census as proof.
**That conclusion is wrong**, and its own subject matter refutes it — the counter-evidence is in the shipped
`shared_prefs/gamedata.xml` plus the serializers in the binary:

| key (verified present in `com.square_enix.android_googleplay.FFBEWW/shared_prefs/gamedata.xml`) | what it holds | serializer (`[FFBEWW]`) |
|---|---|---|
| `keyBATTLE_SAVE_KEY_REPEAT_0` (and `…_SURVIVAL`) | **per-slot `PlayerUnit` battle state** — repeat/alliance battles | `BattleSaveRepeat::{getItemStr,parseItemStr,getRepeatInfoStr,parseRepeatInfoStr}`; `saveAllianceData`/`loadAllianceData` build the key as `prefix + "_" + IntToString(i)` |
| `keytuto_step` | tutorial progress | — |
| `keylast_playing_mission_id`, `keymission_serial`, `keymission_suspend`, `keySuspendFieldMissionBattle` | mission progress, and a suspended mission/battle | `SuspendManager::suspendTown` / `suspendWaveMission` |
| `FIXED_PARTY_SAVED_UNITS_%d` | saved **fixed-party decks** | `UserPartyDeck::saveFixedPartyDeck` (`0x1d7e6b8`) → `GameUtils::getFixedPartyDataAsString` |

**The channel, measured:** `SaveUtils::saveUserDefaults` has **118** call sites and every one routes through JNI
to `com/square_enix/android_googleplay/FFBEWW/LapisJNI` — i.e. **SharedPreferences, not a file**. Consistent with
that: `FileUtils::writeDataToFile` has **0** game callers and `CommonUtils::saveFile` has **0**; the only other
write channels are cocos2d's `UserDefault` XML (15 sites, all inside cocos2d), the MST index writers
(`SeekableMstList::writeIndexFile`), and `HttpURLConnection::saveResponseCookies`.

**Corrected statement:** the *roster/instance model* (`User*Info`) has **no local serialization**, but the client
**does** keep in-progress state — most importantly a **party record** (`FIXED_PARTY_SAVED_UNITS_%d`), which is a
real local source that the host's party fixture (AX-20) and the offline path (AX-33) can be checked against.
`gamedata.xml` is that store, and it is in the dump. **The lesson worth keeping:** the refuted claim was
*asserted* from a census of key *shapes* (`keyF_*`, `_MST`) without reading a single key *value*; the auditor read
one value per family and found player state in three of them. A count is not a sweep.

**(d) Synthetic server must supply.** The whole player state on connect: roster (`UserUnitInfo*`),
parties, inventory, equipment, materia, vision cards, items, gifts, and then a delta after every
mutating action. Instance fields to populate are exactly the server-supplied ones named in
`FFBE_UNIT_DATA_MODEL.md` §7 (level, EXP, current HP, equipment, learned skills, LB level, buffs,
turn order). Confidence **high** that these are server-sourced, **low** on the wire names.

### 2.4 Party / roster / unit management

**(a) What the client does.** Edits party decks, equips items/materia/vision cards, fuses/awakens/mixes
units, marks favourites, and (on the server) registers party slots used by friends.

**(b) Proving symbols (`[FFBEWW]`).** Requests (each with the endpoint in the appendix):
`PartyDeckEditRequest`, `UnitEquipRequest`, `EquipItemFavoriteRequest`, `MateriaFavoriteRequest`,
`UnitFavoriteRequest`, `UnitMixRequest`, `UnitClassUpRequest`, `UnitNvpClassUpRequest`,
`UnitExClassUpRequest`, `VisionCardMixRequest`, `SacrificeRequest`, `ExbonusUpdateRequest`,
`MasterCardReleaseRequest`, `ReinforcementSettingRequest`, `UnitSwitchUpdate`/`UnitUpdate*`.
Model: `UserUnitInfo::{changeEquip,changeMateria,changeVisionCard,getAttachedParty}`,
`PartyRegisterUtil::addOpeSlot(BaseRequest*, UserPartyRegisterSlotInfo*)`,
`PlayerParty::getBattleUnitWithUserUnitID(std::string)`, `UnitUIList::setUnit(UserUnitInfo*, …)`.

**(c) Data shape.** Request class getters show the payload fields per action (e.g. party deck edits carry
slot/unit ids); response shapes are `UserUnitInfoResponse`-family deltas. Concrete field names unknown.

**(d) Synthetic server must supply.** Accept party/equip/fuse actions and return the updated unit/party
records (the canonical "dispOrder"/slot ordering lives server-side). The client computes stats from
templates + instance state, so the server must at least accept the client's resulting stat lines (see battle).

### 2.5 Battle

**(a) What the client does.** Requests mission admission, submits the turn/result payload at mission
end (including a full stat/equipment/resist history and continue count), and can resume/retire/restart;
in-scene it fires specific requests for continues, boss-death, libra, party change and forced terminate.

**(b) Proving symbols (`[FFBEWW]`).** `BattleScene::{requestContinue,requestBossDead,requestForcedTerminate,requestLibraScene,requestPartyChange}`,
`BattleManager::{requestBossDead,requestUseItem,requestReserveCmdUpdate,requestLibraScene,requestSummonCutin,requestLimitBurstCutin,requestOverDriveCutin,requestSkillCutin}`,
`MissionStartRequest::{getMissionId,getDungeonId,getEventSet,getOwnHelperUserUnitId,getOwnHelperUserBeastId,getReinFriUserId,getReinNrmUserId,getReinTabNo,getReinUnitId,getDungeonBonusType,getDungeonBonusParam,createStartQuestTag}`,
`MissionEndRequest::{createMissionDataHistory,createMissionDataHistory_UnitEquip,createMissionDataHistory_UnitResist,createMissionDataHistory_UnitStats,createContinueCountTag,createEndQuestTag,getTotalPower}`,
`MissionContinueRequest`, `MissionRetireRequest`, `MissionReStartRequest`, `MissionWaveStartRequest`,
`MissionWaveReStartRequest`, `MissionSwitchUpdateRequest`, `MissionUpdateRequest`,
`MissionResumeInfoResponse`, `MissionResultResponse`, `GloryMissionEndRequest`,
`SurvivalMission{Start,End,Entry,Restart,Retire,Return}Request`, `Clsm{Start,End,Entry,Lottery}Request`,
`TfaMission{Start,End}Request`, `PlaybackMission{Start,WaveStart}Request`.
Local suspend state: `keyBATTLE_SAVE_KEY_REPEAT_0`, `keymission_suspend`, `keySuspendFieldMission*`,
`keySuspendWaveMission*`, `keysuspend_restart_flg/cnt`, `keyMissionRepeat`, `keymission_serial`,
`keylast_playing_mission_id`, `keymission_reinforcement{,_visioncard}` (`gamedata.xml`).

**(c) Data shape.** `MissionStartRequest` carries mission/dungeon/event-set ids + reinforcement unit
ids; `MissionEndRequest` carries per-unit stat/equip/resist history, continue count, end-quest tag and a
**total power** value. `MissionResultResponse` returns the outcome/rewards. Field names unknown.

**(d) Synthetic server must supply.** Mission admission, mission-end acceptance + rewards/drops,
resume info, and the continue/retire/restart answers. Community records MissionStart/MissionEnd as the
two endpoints it did document (`[Community]`, `docs/analysis/COMMUNITY_RESEARCH.md` §4) — corroborated
here by the request classes, though the community call is older and may not match v9.0.0 tokens.

### 2.6 Gacha

**(a) What the client does.** Reads banner/rate master data locally, requests a pull/entry, and receives
the **results** from the server (the client never rolls). Also handles boxes/panels/select-tickets,
rerolls, prism exchange and ad-gacha milestones.

**(b) Proving symbols (`[FFBEWW]`).** `GachaExeRequest::createPurchaseTag`, `GachaInfoRequest(int,int)`,
`GachaEntryRequest`, `GachaBoxNextRequest`, `GachaPanelNextRequest`, `GachaSelectEntryRequest`,
`GachaSelectExeRequest`, `GachaSelectExchangeExeRequest`, `sgGachaSelectPrismExeRequest`,
`sgGachaRerollExeRequest`, `sgAdsGachaMilestoneRequest`, `RoutineGachaUpdateRequest`,
`updateGachaInfoRequest`, `createGachaRequest`, `createGachaBannerDownloadRequest`;
`GachaExeResponse`, `GachaResultDrawData`, `GachaUnitInfo`, `GachaPanelResultResponse`,
`UserGachaInfoResponse`, `UserGachaBoxInfoResponse`, `UserGachaPanelInfoResponse`,
`UserGachaStepInfoResponse`, `UserGachaDiscountInfoResponse`, `UserGachaLimitedInfoResponse`,
`UserGachaSelectInfoResponse`; MST `F_GACHA_STORE_MST`, `F_GACHA_DETAIL_EXTRA_MST`,
`F_GACHA_EFFECT_BLOCK_MST`, `F_GACHA_EFFECT_PATTERN_MST`, `F_GACHA_SELECT_UNIT_MST`,
`F_GACHA_SELECT_PRISM_MST`, `F_GACHA_WHEEL_EFFECT_MST`; static URL key `GACHA_RATE_URL`.

**(c) Data shape.** Request carries gacha/detail/bundle ids + a purchase tag; response is a draw list
(`GachaResultDrawData` / `GachaUnitInfo`).
**Corrected 2026-09-16 — the *pool* and the *rates* are server-side too, not client-side MST.** The client's gacha
model is banner chrome and cost only: `GachaDetailMst` has **25** setters (banner info/bg/bgm, cost, key
item/dialog, box count, exe type/limit, panel) and **no** rate or weight member (`nm -DC --defined-only
libgame.so | grep -oE 'GachaDetailMst::set[A-Za-z0-9_]+' | sort -u` → 25, none rate-shaped); `GachaMst`'s only
rate-shaped member is `setUpsetRate`, unexplained. Both tables arrive as **server-pushed MST**
(`GachaMstResponse::readParam` `0x196e6cc`) and neither is among the 266 bundled `files/mst/*.dat`, so the pool
never shipped. Consequence: the rates *displayed* in-game were rendering server data, so the**now-corrected** "client-side rate tables" phrasing here and in (d) was wrong, and the legal-disclosure
numbers are not recoverable from the APK.

**(d) Synthetic server must supply.** Banner/step/box state, and for each pull the **drawn unit ids**
plus the resulting `UserUnitInfo` additions. This is the one area where the server is the RNG of
record; the client holds **no** rate table at all (see (c)), only `GachaMst::setUpsetRate`.

### 2.7 Shop / IAP / premium currency ("Lapis", "Diamond")

**(a) What the client does.** Runs a full purchase state machine (Google Play / MyCard), validates its
own receipt locally-ish, then reports purchase start/settlement/hold/fail/give-up/cancel to the game
server; spends premium currency in shops.

**(b) Proving symbols (`[FFBEWW]`).** Requests: `PurchaseStartRequest`, `PurchaseSettlementRequest`,
`PurchaseCancelRequest`, `PurchaseFailedRequest`, `PurchaseGiveUpRequest`, `PurchaseHoldRequest`,
`PurchaseListRequest`, `PurchaseSettingRequest`, `PurchaseCurrentStateRequest`,
`BundlePurchaseRequest`, `BundleStatusRequest`, `ShopUseRequest`, `ShopExchangeItem{Request,List}`,
`ShopExchangeUnitRequest`, `ExchangeShopRequest`, `VariableStoreCheckRequest`,
`MyCardInitializeRequest`, `sgStoreLogReportRequest`.
Responses/UI: `PurchaseSettlementResponse`, `MyCardInfoResponse`, `UserDiamondInfoResponse`,
`UserPurchaseListInfo`, `BuyCoinEndScene::setAccessPurchasePhp`, `CommonUtils::getPurchasePhpUrl()`,
`ShopTopScene::setChangeBuyLapisFlg`, `EquipmentGrowUIOrbCommon::onBuyLapis`,
`sgShopExpansionTicketPrompt::getLapisRequired`, `LapisRecoveryShopId`, `LapisShopPeriod`.
Platform bridge: `Java_com_square_1enix_android_1googleplay_FFBEWW_LapisJNI_{googlePurchasePendingCallback,purchaseStateChangedCallback,resetGooglePurchaseFlags,syncProductPrices,rlog,rlogs}`.
`UserInfo` purchase fields: `getPurchaseReceipt/Signature/TransactionID/ProductId/BundleId/FixFlg/RstFlg/Result/ModelStatus`.

**(c) Data shape.** Purchase requests carry diamond/bundle/gacha ids + receipt/signature/transaction;
the settlement response corrects the currency ledger. `PurchaseFailed/GiveUp/Hold` carry the local
`UserPurchaseListInfo` + `DiaMst`. Endpoint note: `PurchaseStartRequest`, `BundlePurchaseRequest` and
`BundleStatusRequest` **share** `/actionSymbol/tPc64qmn.php`; `MyCardInitializeRequest` has its own path.

**(d) Synthetic server must supply.** Receipt settlement, a Lapis/Diamond ledger, shop stock and
exchange answers, bundle state, and the MyCard path for that region. Server-side receipt verification is
implied by `purchaseFixFlg`/`purchaseRstFlg` but not proven.

### 2.8 Friends, missions, events, news

**(a) What the client does.** Lists/searches/agrees/refuses/deletes/favourites friends, fetches a
friend's leader unit and vision card as a borrowable helper, sets its own reinforcement, and polls
"routine" updates for events/home/world/raid menus plus notices and the home marquee.

**(b) Proving symbols (`[FFBEWW]`).** `FriendListRequest`, `FriendSearchRequest`, `FriendAgreeRequest`,
`FriendRefuseRequest`, `FriendDeleteRequest`, `FriendDetailRequest`, `FriendFavoriteRequest`,
`FriendSuggestRequest`, `FacebookAddFriendRequest`, `MissionResultFriendRequest`,
`ResultFriendRequest`, `GetReinforcementInfoRequest`, `ReinforcementSettingRequest`;
`FriendUnitInfoResponse`, `FriendVisionCardInfoResponse`, `FriendSearchResultResponse`,
`FriendRestartResponse`, `ReinforcementResponse`, `ReinforcementVisionCardInfoResponse`,
`OpeFriendInfoResponse`; `UserInfo::FRIEND_ID_ENCODE_KEY`, `UserInfo::getEncodedFriendID`,
`BaseRequest::getFriendLeaderUpdateInfo()`.
Events/news: `RoutineEventUpdateRequest`, `RoutineHomeUpdateRequest`, `RoutineWorldUpdateRequest`,
`RoutineRaidMenuUpdateRequest`, `NoticeUpdateRequest`, `NoticeReadUpdateRequest`, `NoticeMstResponse`,
`NoticeScheduleMstResponse`, `sgHomeMarqueeInfoRequest/Response`, `sgHomeMarqueeInfo::setDownloadUrl`,
`sgHomeMarqueeManager`, `BannerMst::getUrl()`, `BannerScheduleMstResponse`, `EventSetMstResponse`,
season/parade/glory/rm/tower event `*InfoResponse` classes.
URL dictionary: `EnvUrlMst::{addObject,replaceUrlKey,shared}`, `EnvUrlMstResponse::readParam`,
`UrlInfo::{parseUrlInfo,getUrl,getIconName}`, `F_URL_MST`; keys `P_GS_APPURL_CMN/FB/KKT/LINE/TWITTER/WA`,
`P_GS_NOTICE_HOST_SERVER`, `P_GS_NOTICE_STATIC`, `P_GS_BANNER_PATH`, `P_GS_EVENT_PATH`, `P_GS_CDN_PATH`,
`P_GS_LOGIN_BANNER_VERSION`, `P_GS_FEATURE_*`.

**(c) Data shape.** Friend borrow payloads are `FriendUnitInfo`/`FriendVisionCardInfo` (a unit snapshot);
`specific Friend ID` is sent **encoded** (`FRIEND_ID_ENCODE_KEY`). `EnvUrlMst` is a key→URL dictionary
the server replaces wholesale.

**(d) Synthetic server must supply.** Friend list + leader snapshots, reinforcement responses, routine
event/home/world/raid deltas, notice lists and read-state, home-marquee banner URLs, and — critically —
**`F_URL_MST` / `EnvUrlMst`**: the server pushes URL overrides (`P_GS_*`) that steer CDN, notice and
banner hosts. This is the intended way to repoint a reconstruction.

### 2.9 Localized text / MST updates

**(a) What the client does.** Downloads and validates a set of localized-text MST tables separate from
the main MST set, per language (held language here: `LANGUAGE_ENGLISH`).

**(b) Proving symbols / files (`[FFBEWW]`).**
`sgSplitLocalizedTextMstList::{getMstName,getMstFileName,getMstFileKey,createParser,getValidTextMstList,isRegisteredTextMst,onUpdatedMstList,addForDownload}`,
`sgSplitLocalizedTextMstResponse`, `DownloadSequencePopup::requestLocalizedTextMstDownload`,
`LoginScene::getSkipMstLoad`, MST name `F_LOCALIZED_TEXT_MST`;
on disk `/home/thetb/projects/ffbe/com.square_enix.android_googleplay.FFBEWW/files/localized_texts/en/`
= **145 `.dat`**.

**(c) Data shape.** Same `Ver<N>_<token>.dat` envelope; the format (base64 → AES-128-ECB → PKCS5 →
UTF-8, 144 pipe-delimited + 1 JSON/NDJSON) is already specified in
`docs/analysis/LOCALIZED_TEXT_FORMAT.md` — do not duplicate it here.
**Corrected 2026-09-18:** the *client's* decoder is **CBC-first** (`AES/CBC/PKCS5`, IV the hardcoded
`dZMjkk8gFDzKHlsx`, key = the table's 8-char compiled-in constant NUL-padded to 16) and falls back to ECB
only after the CBC attempt throws — silently, logging nothing. Both encodings are present in the shipped
tree (52 of the 155 tables loaded live are ECB), so "AES-128-ECB" alone is not the whole picture. Evidence:
`/home/thetb/projects/ffbe/APK_emulator_work/docs/notes/base64_decode_path.md` (Appendix, incl. a validated
lossless ECB→CBC transcode).

**(d) Synthetic server must supply.** Current text-table versions + the encrypted `.dat` payloads for
the client's language (`LANGUAGE_ENGLISH`), and answer `sgSplitLocalizedTextMstResponse`.

### 2.10 Telemetry / reporting / third-party SDKs — **not the game server**

**(a) What the client does.** Spools an encrypted report stream and uploads it; also fires analytics
events to several third-party SDKs.

**(b) Proving symbols / files (`[FFBEWW]`).**
`CommonUtils::sendClientReportRequest(std::string const&)`, `ClientReportRequest::{createBody,createUserInfoTag,getEncodeKey,getRequestID,getUrl}`
→ **`/actionSymbol/RntCZywQ.php`** (so client reports *do* traverse the game server, not only third parties).
`LapisAnalytics::{init,track…,batchUpdateItemObtained,batchUpdateItemUsed,…}`,
`AnalyticsManager::{trackEvent,trackPurchase,uploadEvents,setUserId,setDeviceId}`,
`firebase::analytics::*`, `LapisJNI_rlog/rlogs`, `sgStoreLogReportRequest`.
Spool on disk: `/home/thetb/projects/ffbe/com.square_enix.android_googleplay.FFBEWW/files/reports/20241026.log`
— 995,490 bytes, **0 newline bytes**, 3,919 non-base64 bytes (base64-armoured encrypted records).
Third-party SDK state under `shared_prefs/`: `admob.xml`, `appsflyer-data.xml`,
`com.facebook.ads.*` + `FBAdPrefs.xml`, `com.google.android.gms.measurement.prefs.xml` (Firebase
Analytics), `tapjoyCacheData.xml` + `tjcPrefrences.xml`, `unityads-installinfo.xml`,
`supersonic_shared_preferen.xml` (Supersonic/IronSource), `fiverocks.xml` (Fyber),
`__hs_lite_sdk_store.xml`, `paid_storage_sp.xml`, `pcvmspf.xml`.

**(c)/(d)** A synthetic server must answer `RntCZywQ.php` (accept-and-ignore is probably enough; not
verified) and can ignore the ads/analytics SDK backends entirely — those are distinct hosts and the
client's tolerance for their failure is **not verified**.

---

## 3. Endpoint appendix — measured map (Request class → request-ID key → encode key → URL)

Method: for every `<Class>::getRequestID()`, `<Class>::getEncodeKey()` and `<Class>::getUrl()` symbol,
disassemble the stub and resolve its `adrp`+`add` string target. Verified spot-check:
`MissionStartRequest::getUrl()` @ `0x19a8f1c` → `adrp x0,0x2f2f000` / `add x0,x0,#0xf26` →
string at `0x2f2ff26` = `/actionSymbol/63VqtzbQ.php`, matching the table. **High** confidence.

Coverage: **222** request classes mapped, resolving to **211** distinct URLs (10 URLs are shared by
2–3 classes). One URL is the literal `/actionSymbol/action.php` (`GetBackgroundDownloadInfoRequest`,
`UpdateDeviceIdRequest`). `ClientReportRequest → /actionSymbol/RntCZywQ.php`.

| Request class | requestID key | encode key | URL path |
|---|---|---|---|
| `AllianceDeckEditRequest` | `P76LYXow` | `2E3UinsJ` | `/actionSymbol/7gAGFC4I.php` |
| `AllianceEntryRequest` | `HtR8XF4e` | `zS4tPgi7` | `/actionSymbol/EzfT0wX6.php` |
| `AllianceUndercoverStrengthenRequest` | `qj63QmHE` | `75pZA8tv` | `/actionSymbol/UR1OtKJS.php` |
| `ArchiveUpdateRequest` | `cVTxW0K3` | `IFLW9H4M` | `/actionSymbol/2bCcKx0D.php` |
| `BeastBoardPieceOpenRequest` | `0gk3Tfbz` | `7uxYTm3k` | `/actionSymbol/Y2Zvnad9.php` |
| `BeastMixRequest` | `C8X1KUpV` | `WfNSmy98` | `/actionSymbol/7vHqNPF0.php` |
| `BundlePurchaseRequest` | `w6Z9a6tD` | `NE3Pp4K8` | `/actionSymbol/tPc64qmn.php` |
| `BundleStatusRequest` | `uLXAMvCT` | `PrSPuc8c` | `/actionSymbol/tPc64qmn.php` |
| `CampaignTieupRequest` | `mI0Q2YhW` | `72d5UTNC` | `/actionSymbol/2u30vqfY.php` |
| `ChallengeClearRequest` | `D9xphQ8X` | `UD5QCa2s` | `/actionSymbol/dEvLKchl.php` |
| `ClientReportRequest` | `Uq4gcH1s` | `jw84xYPr` | `/actionSymbol/RntCZywQ.php` |
| `ClsmEndRequest` | `3zgbapQ7` | `6aBHXGv4` | `/actionSymbol/7vHqNPF0.php` |
| `ClsmEntryRequest` | `5g0vWZFq` | `8bmHF3Cz` | `/actionSymbol/UmLwv56W.php` |
| `ClsmLotteryRequest` | `Un16HuNI` | `pU62SkhJ` | `/actionSymbol/4uj3NhUQ.php` |
| `ClsmStartRequest` | `4uCSA3ko` | `wdSs23yW` | `/actionSymbol/rncR9js8.php` |
| `CraftAddRequest` | `QkN1Sp64` | `qz0SG1Ay` | `/actionSymbol/iQ7R4CFB.php` |
| `CraftCancelRequest` | `79xDN1Mw` | `68zcUF3E` | `/actionSymbol/7WdDLIE4.php` |
| `CraftEndRequest` | `WIuvh09n` | `yD97t8kB` | `/actionSymbol/9G7Vc8Ny.php` |
| `CraftExeRequest` | `PKDhIN34` | `ZbHEB15J` | `/actionSymbol/UyHLjV60.php` |
| `CraftStartRequest` | `Gr9zxXk5` | `K92H8wkY` | `/actionSymbol/w71MZ0Gg.php` |
| `CreateUserRequest` | `P6pTz4WA` | `73BUnZEr` | `/actionSymbol/0FK8NJRX.php` |
| `DailyDungeonSelectRequest` | `JyfxY2e0` | `ioC6zqG1` | `/actionSymbol/9LgmdR0v.php` |
| `DailyQuestClaimAllRewardRequest` | `DCmya9WD` | `KHx6JdrT` | `/actionSymbol/Br9PwJ6A.php` |
| `DailyQuestClaimRewardRequest` | `Zy8fYJ5e` | `jwYGF3sY` | `/actionSymbol/Br9PwJ6A.php` |
| `DailyQuestShareRequest` | `3sTwRcpq` | `PMyGMdUa` | `/actionSymbol/3sTwRcpq.php` |
| `DailyQuestUpdateRequest` | `6QYd5Hym` | `9QtGVCWg` | `/actionSymbol/QWDn5epF.php` |
| `DmgRankEndRequest` | `s98cw1WA` | `7pGj8hSW` | `/actionSymbol/zd5KJ3jn.php` |
| `DmgRankRetireRequest` | `W3Z4VF1X` | `5fkWyeE6` | `/actionSymbol/8wdmR9yG.php` |
| `DmgRankStartRequest` | `5P6ULvjg` | `1d5AP9p6` | `/actionSymbol/j37Vk5xe.php` |
| `DungeonLiberationRequest` | `nQMb2L4h` | `0xDA4Cr9` | `/actionSymbol/0vc6irBY.php` |
| `DungeonResourceLoadMstListRequest` | `jnw49dUq` | `3PVu6ReZ` | `/actionSymbol/Sl8UgmP4.php` |
| `EquipGrowAbilityFixRequest` | `k8ew94DN` | `58dS0DZN` | `/actionSymbol/CnPyXkUV.php` |
| `EquipGrowAbilitySelectResumeRequest` | `80R6BXUw` | `7YxgkK1V` | `/actionSymbol/Ke7YG3xW.php` |
| `EquipGrowEntryRequest` | `U8F0Q25i` | `6fTy3HRM` | `/actionSymbol/UiSOVXT2.php` |
| `EquipItemFavoriteRequest` | `cVRHU84e` | `dRBYF8Q0` | `/actionSymbol/fZ4jONfh.php` |
| `ExbonusExeRequest` | `68RQ9GwN` | `FvL0AP1U` | `/actionSymbol/u843ypX2.php` |
| `ExbonusUpdateRequest` | `HaJp14F6` | `a3BP2MRo` | `/actionSymbol/lNJqXG3D.php` |
| `ExchangeShopRequest` | `I7fmVX3R` | `qoRP87Fw` | `/actionSymbol/1bf0HF4w.php` |
| `FacebookAddFriendRequest` | `NAW9vJnm` | `532vAYUy` | `/actionSymbol/NAW9vJnm.php` |
| `FacebookLogoutRequest` | `xHTo4BZp` | `wwHxtAy6` | `/actionSymbol/xHTo4BZp.php` |
| `FacebookRewardClaimRequest` | `47R9pLGq` | `Rja82ZUK` | `/actionSymbol/47R9pLGq.php` |
| `FacebookRewardListRequest` | `8YZsGLED` | `85YBRzZg` | `/actionSymbol/8YZsGLED.php` |
| `FriendAgreeRequest` | `kx13SLUY` | `9FjK0zM3` | `/actionSymbol/1DYp5Nqm.php` |
| `FriendDeleteRequest` | `a2d6omAy` | `d0VP5ia6` | `/actionSymbol/8R4fQbYh.php` |
| `FriendDetailRequest` | `7kG0JAvE` | `aKvkU6Y4` | `/actionSymbol/QBiJEyUt.php` |
| `FriendFavoriteRequest` | `1oE3Fwn4` | `3EBXbj1d` | `/actionSymbol/8IYSJ5H1.php` |
| `FriendListRequest` | `u7Id4bMg` | `1iV2oN9r` | `/actionSymbol/p3hwqW5U.php` |
| `FriendRefuseRequest` | `1nbWRV9w` | `RYdX9h2A` | `/actionSymbol/Vw0a4I3i.php` |
| `FriendRequest` | `j0A5vQd8` | `6WAkj0IH` | `/actionSymbol/8drhF2mG.php` |
| `FriendSearchRequest` | `3siZRSU4` | `VCL5oj6u` | `/actionSymbol/6Y1jM3Wp.php` |
| `FriendSuggestRequest` | `iAs67PhJ` | `j2P3uqRC` | `/actionSymbol/6TCn0BFh.php` |
| `GachaBoxNextRequest` | `xa9uR3pI` | `U2Lm8vcS` | `/actionSymbol/tULiKh5j.php` |
| `GachaEntryRequest` | `rj6dxU9w` | `39cFjtId` | `/actionSymbol/tUJxSQz7.php` |
| `GachaExeRequest` | `9fVIioy1` | `oaEJ9y1Z` | `/actionSymbol/oC30VTFp.php` |
| `GachaInfoRequest` | `UNP1GR5n` | `VA8QR57X` | `/actionSymbol/3nhWq25K.php` |
| `GachaPanelNextRequest` | `32YLaEmn` | `qbHn0E2L` | `/actionSymbol/7WGJtJwo.php` |
| `GachaSelectEntryRequest` | `6nTcSp4R` | `uG1jRky6` | `/actionSymbol/6RI813OP.php` |
| `GachaSelectExchangeExeRequest` | `W58vo4Hp` | `98yAYp5c` | `/actionSymbol/ROLuCfi2.php` |
| `GachaSelectExeRequest` | `xio14KrL` | `BuJqHc41` | `/actionSymbol/eB0VYGMt.php` |
| `GameSettingRequest` | `OTX6Fmvu` | `4foXVwWd` | `/actionSymbol/OTX6Fmvu.php` |
| `GetBackgroundDownloadInfoRequest` | `lEHBdOEf` | `Z1krd75o` | `/actionSymbol/action.php` |
| `GetReinforcementInfoRequest` | `AJhnI37s` | `87khNMou` | `/actionSymbol/hXMoLwgE.php` |
| `GetTitleInfoRequest` | `ocP3A1FI` | `Mw56RNZ2` | `/actionSymbol/BbIeq31M.php` |
| `GetUserInfo2Request` | `2eK5Vkr8` | `7VNRi6Dk` | `/actionSymbol/7KZ4Wvuw.php` |
| `GetUserInfo3Request` | `4rjw5pnv` | `0Dn4hbWC` | `/actionSymbol/lZXr14iy.php` |
| `GetUserInfoRequest` | `X07iYtp5` | `rcsq2eG7` | `/actionSymbol/u7sHDCg4.php` |
| `GiftUpdateRequest` | `9KN5rcwj` | `xLEtf78b` | `/actionSymbol/noN8I0UK.php` |
| `GloryConfirmedGimmickRequest` | `7Bt6HTw0` | `4nik5jwd` | `/actionSymbol/F86LHcMO.php` |
| `GloryEntryRequest` | `UDb5Je1j` | `FtD9IGj7` | `/actionSymbol/nEZ9twZH.php` |
| `GloryMissionEndRequest` | `BQG1rm3y` | `5xvUq9Gm` | `/actionSymbol/F86LHcMO.php` |
| `GloryMissionReStartRequest` | `YAF1wt9J` | `fxCuP4k3` | `/actionSymbol/DHpM2abg.php` |
| `GloryMissionStartRequest` | `oj76TPS2` | `5hqtxF36` | `/actionSymbol/hZ4GFCAH.php` |
| `GooglePromotionRewardRequest` | `6PDhpa5T` | `F7AMb1u2` | `/actionSymbol/jSxaKcr0.php` |
| `GrandMissionEntryRequest` | `MTf2j9aK` | `Uey5jW2G` | `/actionSymbol/8DermCsY.php` |
| `InitializeRequest` | `75fYdNxq` | `rVG09Xnt` | `/actionSymbol/fSG1eXI9.php` |
| `IsNeedValidateRequest` | `er5xMIj6` | `djhiU6x8` | `/actionSymbol/gk3Wtr8A.php` |
| `ItemBuyRequest` | `sxK2HG6T` | `InN5PUR0` | `/actionSymbol/oQrAys71.php` |
| `ItemCarryEditRequest` | `UM7hA0Zd` | `04opy1kf` | `/actionSymbol/8BE6tJbf.php` |
| `ItemSellRequest` | `d9Si7TYm` | `E8H3UerF` | `/actionSymbol/hQRf8D6r.php` |
| `LibraryVisionCardEntryRequest` | `Lx8F2iU0` | `63ucryZ7` | `/actionSymbol/lIUfgWYj.php` |
| `LoginBonusRequest` | `vw9RP3i4` | `Vi6vd9zG` | `/actionSymbol/iP9ogKy6.php` |
| `MailListRequest` | `KQHpi0D7` | `7kgsrGQ1` | `/actionSymbol/u3E8hpad.php` |
| `MailReceiptRequest` | `XK7efER9` | `P2YFr7N9` | `/actionSymbol/M2fHBe9d.php` |
| `MasterCardReleaseRequest` | `23fbcXt4` | `7JE6MzkC` | `/actionSymbol/sC1oeYeD.php` |
| `MateriaFavoriteRequest` | `k5v0hMd8` | `La3si8kz` | `/actionSymbol/lW4xfpvS.php` |
| `MedalExchangeRequest` | `LiM9Had2` | `dCja1E54` | `/actionSymbol/0X8Fpjhb.php` |
| `MissionBreakRequest` | `17LFJD0b` | `Z2oPiE6p` | `/actionSymbol/P4oIeVf0.php` |
| `MissionContinueRequest` | `LuCN4tU5` | `34n2iv7z` | `/actionSymbol/ZzCXI6E7.php` |
| `MissionContinueRetireRequest` | `V3CiWT0r` | `F1QRxT5m` | `/actionSymbol/cQU1D9Nx.php` |
| `MissionEndRequest` | `x5Unqg2d` | `1tg0Lsqj` | `/actionSymbol/0ydjM5sU.php` |
| `MissionReStartRequest` | `GfI4LaU3` | `Vw6bP0rN` | `/actionSymbol/r5vfM1Y3.php` |
| `MissionRetireRequest` | `v51PM7wj` | `oUh1grm8` | `/actionSymbol/gbZ64SQ2.php` |
| `MissionStartRequest` | `29JRaDbd` | `i48eAVL6` | `/actionSymbol/63VqtzbQ.php` |
| `MissionSwitchUpdateRequest` | `Tvq54dx6` | `bZezA63a` | `/actionSymbol/1Xz8kJLr.php` |
| `MissionUpdateRequest` | `j5JHKq6S` | `Nq9uKGP7` | `/actionSymbol/fRDUy3E2.php` |
| `MissionWaveReStartRequest` | `e9RP8Cto` | `M3bYZoU5` | `/actionSymbol/8m7KNezI.php` |
| `MissionWaveStartRequest` | `BSq28mwY` | `d2mqJ6pT` | `/actionSymbol/Mn15zmDZ.php` |
| `MyCardInitializeRequest` | `BruItbuW` | `q54ajlRb` | `/actionSymbol/d6f9LSI7.php` |
| `NoticeReadUpdateRequest` | `pC3a2JWU` | `iLdaq6j2` | `/actionSymbol/j6kSWR3q.php` |
| `NoticeUpdateRequest` | `CQ4jTm2F` | `9t68YyjT` | `/actionSymbol/TqtzK84R.php` |
| `NvSacrificeRequest` | `x94MnujU` | `KXcC8A1o` | `/actionSymbol/VliYfNhr.php` |
| `OptionUpdateRequest` | `otgXV79T` | `B9mAa7rp` | `/actionSymbol/0Xh2ri5E.php` |
| `ParadeEntryRequest` | `Q46YTgWo` | `kF7Z5vCK` | `/actionSymbol/9i4b61rU.php` |
| `ParadeMissionEndRequest` | `4cACW7y3` | `Pv08w12y` | `/actionSymbol/hC8r9p81.php` |
| `PartyDeckEditRequest` | `TS5Dx9aZ` | `34qFNPf7` | `/actionSymbol/6xkK4eDG.php` |
| `PartyRegisterSlotLoadRequest` | `Q7jV6pno` | `7zcaE16T` | `/actionSymbol/2y4BVhyj.php` |
| `PartyRegisterSlotSaveRequest` | `CE98eghN` | `0S9Uzr42` | `/actionSymbol/QjDQEMYc.php` |
| `PartyRegisterSlotUpdateRequest` | `RQ5BjDE0` | `CpZH8rY0` | `/actionSymbol/HgSTAzd2.php` |
| `PlaybackMissionStartRequest` | `1YnQM4iB` | `YC20v1Uj` | `/actionSymbol/zm2ip59f.php` |
| `PlaybackMissionWaveStartRequest` | `1BpXP3Fs` | `NdkX15vE` | `/actionSymbol/scyPYa81.php` |
| `PlayerEmblemEntryRequest` | `Z7J9H6TK` | `F7A2MJoE` | `/actionSymbol/huKNdci6.php` |
| `PlayerEmblemSettingRequest` | `19YwqU2T` | `76kGLIgN` | `/actionSymbol/cswGxj8F.php` |
| `PurchaseCancelRequest` | `L7K0ezU2` | `Z1mojg9a` | `/actionSymbol/y71uBCER.php` |
| `PurchaseCurrentStateRequest` | `9mM3eXgi` | `X9k5vFdu` | `/actionSymbol/bAR4k7Qd.php` |
| `PurchaseFailedRequest` | `jSe80Gx7` | `sW0vf3ZM` | `/actionSymbol/2TCis0R6.php` |
| `PurchaseGiveUpRequest` | `BFf1nwh6` | `xoZ62QWy` | `/actionSymbol/C2w0f3go.php` |
| `PurchaseHoldRequest` | `79EVRjeM` | `5Mwfq90Z` | `/actionSymbol/dCxtMZ27.php` |
| `PurchaseListRequest` | `BT28S96F` | `X3Csghu0` | `/actionSymbol/YqZ6Qc1z.php` |
| `PurchaseSettingRequest` | `QkwU4aD9` | `ePFcMX53` | `/actionSymbol/9hUtW0F8.php` |
| `PurchaseSettlementRequest` | `JsFd4b7j` | `jmh7xID8` | `/actionSymbol/yt82BRwk.php` |
| `PurchaseStartRequest` | `qAUzP3R6` | `9Kf4gYvm` | `/actionSymbol/tPc64qmn.php` |
| `RateAppRewardRequest` | `L0OsxMaT` | `m1pPBwC3` | `/actionSymbol/L0OsxMaT.php` |
| `RbBoardPieceOpenRequest` | `hqzU9Qc5` | `g68FW4k1` | `/actionSymbol/iXKfI4v1.php` |
| `RbEndRequest` | `os4k7C0b` | `MVA3Te2i` | `/actionSymbol/e8AHNiT7.php` |
| `RbEntryRequest` | `f8kXGWy0` | `EA5amS29` | `/actionSymbol/30inL7I6.php` |
| `RbMatchingRequest` | `DgG4Cy0F` | `4GSMn0qb` | `/actionSymbol/mn5cHaJ0.php` |
| `RbRankingRequest` | `kcW85SfU` | `SR6PoLM3` | `/actionSymbol/3fd8y7W1.php` |
| `RbReStartRequest` | `6ZNY3zAm` | `PRzAL3V2` | `/actionSymbol/DQ49vsGL.php` |
| `RbStartRequest` | `eHY7X8Nn` | `P1w8BKLI` | `/actionSymbol/dR20sWwE.php` |
| `ReinforcementSettingRequest` | `ZSq2y7EX` | `jUreV31B` | `/actionSymbol/I1g4ezbP.php` |
| `ResourceAllDownloadRequest` | `i0d5n1Dp` | `fL07ojUc` | `/actionSymbol/Vtx9kFg0.php` |
| `RmDungeonEndRequest` | `WaPC2T6i` | `dEnsQ75t` | `/actionSymbol/CH9fWn8K.php` |
| `RmDungeonStartRequest` | `R5mWbQ3M` | `A7V1zkyc` | `/actionSymbol/NC8Ie07P.php` |
| `RmEndRequest` | `fyp10Rrc` | `FX5L3Sfv` | `/actionSymbol/I9p3n48A.php` |
| `RmEntryRequest` | `wx5sg9ye` | `p2tqP7Ng` | `/actionSymbol/fBn58ApV.php` |
| `RmRestartRequest` | `yh21MTaG` | `R1VjnNx0` | `/actionSymbol/NC8Ie07P.php` |
| `RmRetireRequest` | `e0R3iDm1` | `T4Undsr6` | `/actionSymbol/fBn58ApV.php` |
| `RmStartRequest` | `7FyJS3Zn` | `iu67waph` | `/actionSymbol/8BJSL7g0.php` |
| `RoutineEventUpdateRequest` | `4kA1Ne05` | `V0TGwId5` | `/actionSymbol/WCK5tvr0.php` |
| `RoutineGachaUpdateRequest` | `t60dQP49` | `Q6ZGJj0h` | `/actionSymbol/qS0YW57G.php` |
| `RoutineHomeUpdateRequest` | `Daud71Hn` | `aw0syG7H` | `/actionSymbol/1YWTzU9h.php` |
| `RoutineRaidMenuUpdateRequest` | `g0BjrU5D` | `z80swWd9` | `/actionSymbol/Sv85kcPQ.php` |
| `RoutineWorldUpdateRequest` | `6H1R9WID` | `XDIL4E7j` | `/actionSymbol/oR1psQ5B.php` |
| `SacrificeRequest` | `7tWdn9zH` | `U80FYThX` | `/actionSymbol/QBiJEyUt.php` |
| `SearchGetItemInfoRequest` | `0D9mpGUR` | `vK2V8mZM` | `/actionSymbol/e4Gjkf0x.php` |
| `ShopExchangeItemListRequest` | `syKz34cE` | `h69WSu02` | `/actionSymbol/7KJjJiIh.php` |
| `ShopExchangeItemRequest` | `xD5b6PqQ` | `vaDW85R2` | `/actionSymbol/qhP5wSXV.php` |
| `ShopExchangeUnitRequest` | `Vgi7j68T` | `x6rSuK0J` | `/actionSymbol/lnXYChmF.php` |
| `ShopUseRequest` | `73SD2aMR` | `ZT0Ua4wL` | `/actionSymbol/w76ThDMm.php` |
| `SignInCheckRequest` | `ufKRrNc7` | `F83wNKFt` | `/actionSymbol/Qfpa24mZ.php` |
| `SignInRequest` | `FckReppg` | `g8iv4P8I` | `/actionSymbol/8DRAiBXE.php` |
| `SignOutRequest` | `o96pHAp3` | `UtE1qMv3` | `/actionSymbol/KwChAfkX.php` |
| `SpChallengeEntryRequest` | `MTf2j9aK` | `Uey5jW2G` | `/actionSymbol/8DermCsY.php` |
| `SpChallengeRewardGetRequest` | `2G7ZVs4A` | `mG25PIUn` | `/actionSymbol/9inGHyqC.php` |
| `StrongBoxOpenRequest` | `PIv7u8jU` | `sgc30nRh` | `/actionSymbol/48ktHf13.php` |
| `SublimationSkillRequest` | `s48Qzvhd` | `97Uvrdz3` | `/actionSymbol/xG3jBbw5.php` |
| `SurvivalMissionEndRequest` | `3cuPF20R` | `VNj9J3Bg` | `/actionSymbol/HBIJ1BxB.php` |
| `SurvivalMissionEntryRequest` | `cH9DnW03` | `nUfp8v2y` | `/actionSymbol/iAL25ka3.php` |
| `SurvivalMissionRestartRequest` | `p02YN1JA` | `70HoeuUR` | `/actionSymbol/9R2VNtZV.php` |
| `SurvivalMissionRetireRequest` | `yW35iC40` | `YKfeAw01` | `/actionSymbol/T1MnS9pf.php` |
| `SurvivalMissionReturnRequest` | `Ge2ZLDC1` | `0X67saPi` | `/actionSymbol/cGQ3zDn8.php` |
| `SurvivalMissionStartRequest` | `67tFRKyo` | `DT3UHBu1` | `/actionSymbol/RXwRAXmX.php` |
| `TfaBonusRewardInfoRequest` | `fcPxKx8M` | `fga9Awck` | `/actionSymbol/fcPxKx8M.php` |
| `TfaEntryRequest` | `C0Lne0I7` | `OVmfEPwl` | `/actionSymbol/C0Lne0I7.php` |
| `TfaMissionEndRequest` | `JiglLZx6` | `c4d1b068` | `/actionSymbol/JiglLZx6.php` |
| `TfaMissionStartRequest` | `BSq28mwY` | `d2mqJ6pT` | `/actionSymbol/Mn15zmDZ.php` |
| `TfaRankingDetailRequest` | `Bg1cbQV7` | `apV7aUzj` | `/actionSymbol/Bg1cbQV7.php` |
| `TfaResultRewardFixRequest` | `JO2zA1Jy` | `bv2RPd1G` | `/actionSymbol/JO2zA1Jy.php` |
| `TfaResultRewardSelectResumeRequest` | `4qVA8LFK` | `hqwHhgPE` | `/actionSymbol/4qVA8LFK.php` |
| `TowerEndRequest` | `fBN47X2b` | `kzFy73L9` | `/actionSymbol/TZXbYd9b.php` |
| `TowerEntryRequest` | `VfEh2wD0` | `L9d1gYAm` | `/actionSymbol/scI7HnwD.php` |
| `TowerRestartRequest` | `9Z3jCWfF` | `04HMuYV1` | `/actionSymbol/iqxK7alu.php` |
| `TowerRetireRequest` | `MTuX7ai2` | `5dqrT8Mi` | `/actionSymbol/sn4mo4gN.php` |
| `TowerStartRequest` | `dZA90j5s` | `M3dCmDW8` | `/actionSymbol/1ch0bfGj.php` |
| `TownInRequest` | `8EYGrg76` | `JI8zU5rC` | `/actionSymbol/isHfQm09.php` |
| `TownOutRequest` | `sJcMPy04` | `Kc2PXd9D` | `/actionSymbol/0EF3JPjL.php` |
| `TownUpdateRequest` | `G1hQM8Dr` | `37nH21zE` | `/actionSymbol/0ZJzH2qY.php` |
| `TransferCodeCheckRequest` | `CY89mIdz` | `c5aNjK9J` | `/actionSymbol/C9LoeYJ8.php` |
| `TransferCodeIssueRequest` | `crzI2bA5` | `T0y6ij47` | `/actionSymbol/hF0yCKc1.php` |
| `TransferRequest` | `oE5fmZN9` | `C6eHo3wU` | `/actionSymbol/v6Jba7pX.php` |
| `TrophyRewardRequest` | `wukWY4t2` | `2o7kErn1` | `/actionSymbol/05vJDxg9.php` |
| `UnitClassUpRequest` | `zf49XKg8` | `L2sTK0GM` | `/actionSymbol/8z4Z0DUY.php` |
| `UnitEquipRequest` | `pB3st6Tg` | `45VZgFYv` | `/actionSymbol/nIk9z5pT.php` |
| `UnitExClassUpRequest` | `k6NI9Tm1` | `3xNEuAJ0` | `/actionSymbol/P19pJnPf.php` |
| `UnitExchangeRequest` | `3ekgi8bh` | `up94SLAv` | `/actionSymbol/YVZlW2gN.php` |
| `UnitFavoriteRequest` | `tBDi10Ay` | `w9mWkGX0` | `/actionSymbol/sqeRg12M.php` |
| `UnitMixRequest` | `UiSC9y8R` | `4zCuj2hK` | `/actionSymbol/6aLHwhJ8.php` |
| `UnitNvClassUpRequest` | `Pf5C8xjJ` | `2tJE0Ijo` | `/actionSymbol/ukNsKIEo.php` |
| `UnitNvpClassUpRequest` | `V8GtMQ2q` | `m23Etnuz` | `/actionSymbol/5kOVOj0a.php` |
| `UnitSellRequest` | `9itzg1jc` | `DJ43wmds` | `/actionSymbol/0qmzs2gA.php` |
| `UnitStorageUpdateRequest` | `5ik69cuw` | `3HDUxoG5` | `/actionSymbol/HkfCn09z.php` |
| `UnitSublimationRequest` | `a54L9CKY` | `B7iQjmz5` | `/actionSymbol/PiQObNT2.php` |
| `UnitUsePointRequest` | `nsxUW3Y7` | `1iPZp3kJ` | `/actionSymbol/Why4WO1r.php` |
| `UpdateDeviceIdRequest` | `aQm3zP2c` | `mvRSdn8f` | `/actionSymbol/action.php` |
| `UpdateSwitchInfoRequest` | `mRPo5n2j` | `4Z5UNaIW` | `/actionSymbol/SqoB3a1T.php` |
| `UpdateUserInfoRequest` | `ey8mupb4` | `6v5ykfpr` | `/actionSymbol/v3RD1CUB.php` |
| `VariableStoreCheckRequest` | `i0woEP4B` | `Hi0FJU3c` | `/actionSymbol/Nhn93ukW.php` |
| `VisionCardDisplayRequest` | `TmS3EjZ8` | `e3LTk8ti` | `/actionSymbol/mCy1T1hc.php` |
| `VisionCardFavoriteRequest` | `VTbX14dD` | `YkRC3A9U` | `/actionSymbol/PEIMihWu.php` |
| `VisionCardMixRequest` | `5J14x3Qz` | `NVMk7Qr1` | `/actionSymbol/aeBv64rU.php` |
| `sgAdsGachaMilestoneRequest` | `PkSzb2TM` | `Jp4Fz3qb` | `/actionSymbol/PkSzb2TM.php` |
| `sgComebackQuestClaimRewardRequest` | `1IOdaWOW` | `4NO3PIyU` | `/actionSymbol/I7RzrQ3J.php` |
| `sgEmblemEventRequest` | `wMWvnYDR` | `U9QMZrFq` | `/actionSymbol/wMWvnYDR.php` |
| `sgExpdAccelerateRequest` | `Ik142Ff6` | `d3D4l8b4` | `/actionSymbol/Ik142Ff6.php` |
| `sgExpdEndRequest` | `2pe3Xa8b` | `cjHumZ2J` | `/actionSymbol/2pe3Xa8b.php` |
| `sgExpdMileStoneClaimRequest` | `r4A791RF` | `t04N07LQ` | `/actionSymbol/r4A791RF.php` |
| `sgExpdQuestInfoRequest` | `hW0804Q9` | `4Bn7d973` | `/actionSymbol/hW0804Q9.php` |
| `sgExpdQuestRefreshRequest` | `vTgYyHM6` | `vceNlSf3` | `/actionSymbol/vTgYyHM6.php` |
| `sgExpdQuestStartRequest` | `I8uq68c3` | `60Os29Mg` | `/actionSymbol/I8uq68c3.php` |
| `sgExpdRecallRequest` | `0Fb87D0F` | `9J02K0lX` | `/actionSymbol/0Fb87D0F.php` |
| `sgGachaRerollExeRequest` | `Dll4rncD` | `KJ13DlZz` | `/actionSymbol/QQVXslxB.php` |
| `sgGachaSelectPrismExeRequest` | `bgast6dR` | `7mDIdVEI` | `/actionSymbol/pXAIaMKW.php` |
| `sgHiddenSkillUpgrade` | `qxUUaS8s` | `rXDvnsv8` | `/actionSymbol/qxUUaS8s.php` |
| `sgHomeMarqueeInfoRequest` | `PBSP9qn5` | `d3GDS9X8` | `/actionSymbol/PBSP9qn5.php` |
| `sgMissionUnlockRequest` | `LJhqu0x6` | `ZcBV06K4` | `/actionSymbol/LJhqu0x6.php` |
| `sgOfferwallInfoRequest` | `uO1w9ggv` | `QgNR0HvE` | `/actionSymbol/NmNB96p8.php` |
| `sgPanelQuestEntryRequest` | `GVyjSq16` | `T3g6EPOp` | `/actionSymbol/GVyjSq16.php` |
| `sgPanelQuestExtraRewardRequest` | `AOY6npa3` | `vZWKUHhY` | `/actionSymbol/AOY6npa3.php` |
| `sgPanelQuestRewardRequest` | `6uue8aKx` | `bjtzvZ5X` | `/actionSymbol/6uue8aKx.php` |
| `sgPanelQuestStatusGetRequest` | `egJpaGtk` | `uzEklRfc` | `/actionSymbol/egJpaGtk.php` |
| `sgStoreLogReportRequest` | `6611dc2f` | `af7a56a0` | `/actionSymbol/6611dc2f.php` |
| `sgUserExtraChallengeInfoRequest` | `dzU2t3HA` | `Qu6RHqbb` | `/actionSymbol/zgry2aNV.php` |

---

## 4. Not verified — do not assume

- **No request/response JSON schema was recovered.** *(Narrowed 2026-09-16: one endpoint's schema **is** now
  recovered — `APK_emulator_work/docs/notes/userinfo_payload_trees.md`, 26 `UserInfo` response tokens with their offsets and owning
  accessors plus the request-side group tree. The remaining `*Response::readParam` symbols are the same method,
  unrepeated — see §6 step 1.)* `*Response::readParam(tag, type, name, value, flag)`
  exists for 450 response classes but the tag/field *names* are 8-char obfuscated tokens. The community
  `variables.json` / `response.json` maps (BraveHaxvius) are version-older and partial; treat them as
  `[Community]` leads, not a spec.
- **~~Exact crypto envelope unproven~~ — RESOLVED 2026-09-16; the spec is §1e.** All four of the sub-questions this
  bullet used to list are now answered for the **network** path: one key encrypts both the request and the response
  body (the per-request 8-char `EncodeKey`, UTF-8, NUL-padded to 16), and the mode/padding is **AES-128-CBC with
  PKCS5**, invoked in **Java via JNI**. Kept as a retraction record, because the answer was in the repo the whole
  time: `docs/notes/bravehaxvius_assessment.md` §2 stated it, and `scripts/mst_decode.py` had the IV. The
  `SignalKey`'s role is now **settled too (2026-09-23, SW-3): server-issued and echoed verbatim, with no
  signature or verification** — see the lifecycle bullet below.
  (`docs/analysis/LOCALIZED_TEXT_FORMAT.md`).
- **`getRequestID()` vs `getEncodeKey()` semantics.** Both return distinct 8-char tokens; which is a
  JSON field name and which is key material is **not** proven. They are a *different token pool* from the
  community field-name obfuscation tokens (measured), but that does not itself prove their role.
- **`SignalKey` lifecycle — RESOLVED 2026-09-23 (SW-3).** The key arrives in `SignalKeyResponse`, is stored via
  `UserInfo::setSignalKey` (member `UserInfo+0x100`), and is **echoed verbatim** on each request by
  `BaseRequest::createSignalKeyTag` (`0x198f8ac`: `JsonGroup("QCcFB3h9")` → `addParam("qrVcDe48",
  UserInfo+0x100)`). **Nothing signs or verifies it**: `createSignalKeyTag` computes no hash,
  `UserInfo::getSignalKey` (`0x1d8988c`) has 0 direct callers, and no request-signing symbol exists (the
  binary's crypto is the bundled OpenSSL for TLS and CRI for audio). Its relation to `getEncodeKey()` is none —
  the latter is per-request key material, this a session token. **Consequence for the synthetic server: issue it
  and echo it; no validation is required.** (Medium on any additional *server-side* use — the server's semantics
  are not in the client binary — but the client side is settled: echo-only.)
- **Endpoint list completeness.** 211 literals + 222 mapped classes is what is compiled in; a route could
  still be composed at runtime from `EnvUrlMst` (`P_GS_*`) or built by concatenation not visible as a
  literal. Do not claim the list is provably exhaustive.
- **~~TLS pinning~~ — RESOLVED; see §1f. Do not treat this as open.** Kept for provenance: the earlier
  reading was absence-of-evidence only. §1f settles it from the call sites, the daemon-side search and the
  APK's own trust config — and a live client has since verified against a **system-store** CA
  (`APK_emulator_work/docs/HARNESS_PLAN.md` §7).
- **`keyuser_id` / `keyhandle_name` values** in `gamedata.xml` both read `magic_key_saved_as_v2` — a
  sentinel meaning the real value is stored elsewhere (format/keys unknown). Do not read them as real ids.
- **`Sbo89`** appears as a config literal in `CommonUtils::getResourceUrl()`; its meaning is unverified.
- **Server authority.** Whether the server validates/recomputes battle stats, gacha, or currency, or
  merely echoes, is unknown (server code is absent). `MissionEndRequest` uploads a full stat history that
  *looks* auditable, but auditing is not proven.
- **Guest vs platform login state machine** (`addForceGuestLoginChecksum`, `getIsForceLoginAsGuest`) and
  the 2FA/"Tfa" requests' flow were not traced.
- The exact **`Ver<N>` assignment rule**, which tables are mandatory at first boot
  (`LoginScene::MIN_MST_FILE`), and the per-table hash→name mapping (`getMstName`/`getMstFileName`) are
  not decoded.

---

## 5. Verification log

Scratch (allowed): `/tmp/server_contract/` only. Commands run (verbatim results quoted in-line):
`nm -D --defined-only --demangle libgame.so > nm.txt` (112,454 lines);
`strings -n 8 libgame.so` (154,734 lines) and `strings -t x -n 6` (162,847 lines);
`grep -cE '^/actionSymbol/[A-Za-z0-9]+\.php$' strings.txt` = 211;
`llvm-objdump -d --start-address=… --stop-address=… libgame.so` for the URL/key/`PHP_URL`/`getPhpUrl`/
`getFullpath`/`Crypto` evidence; ELF `.rodata` and `.text` have VMA == file offset (confirmed with
`readelf -S`), so string addresses were read directly from the file;
`ls mst/ | wc -l` → 266 files = 219 `.dat` + 47 `.i`; `ls localized_texts/en/ | wc -l` → 145;
`grep -o 'name="[^"]*"' gamedata.xml | wc -l` → 411;
`tr -cd '\n' < reports/20241026.log | wc -c` → 0.
Path existence confirmed for every cited repo file. `docs/COVERAGE_LEDGER.md` was **not** edited.

---

## 6. Recovery plan — the unknowns, in the order a synthetic server hits them

Ranked so each step unblocks the next, and the first proves the method for the other 221 endpoints.

1. **One endpoint's field map, end to end** — `GetUserInfoRequest` / `UserInfoResponse`, the first payload
   on the critical path. Disassemble `createBody()` and `readParam(tag, type, name, value, flag)`; the
   `name` literals *are* the 8-char wire keys, and the compiler kept the `set*`/`get*` names that give the
   semantics — so the map is recoverable by xref, exactly as the MST column hashes were. Verify against
   the client's own parser. **This is the proof step.** **DONE 2026-09-16** — the map is
   `APK_emulator_work/docs/notes/userinfo_payload_trees.md`: 26 response tokens with the `UserInfo` offset **and** the owning
   accessor for each, the request-side group tree, and a "what a fake server must send" section. It also
   **sharpens the boot path**: `GetUserInfo{Request,2Request,3Request}` is **never constructed** in `.text`, so
   the response half is reached by body tag `LhVz6aD2` whichever request carries it — the boot packet is a
   different, still-unidentified request (see that note's unresolved #1). **RESOLVED LIVE 2026-09-17** — the boot
   packet is **`GameSettingRequest`** → `/actionSymbol/OTX6Fmvu.php` (requestID `OTX6Fmvu`, encode key
   `4foXVwWd`), carrying the `LhVz6aD2` / `QCcFB3h9` / `c1qYg84Q` groups. The capture, its decrypted body, and
   the §1e envelope confirmed against a real client are in
   `APK_emulator_work/docs/notes/boot_packet_capture.md`. The *client-side* flow it belongs to is already
   recovered — do not re-derive it: `docs/analysis/FFBE_SCENE_FLOW.md` §2.3 (the boot chain) and
   `axmol_work/docs/screen_ladder.md` (the headless `PostStartupScene` connect step).
2. **The crypto envelope — ANSWERED; the verified spec is §1e** (this item tracks only what remains).
   **Original write-up kept below for provenance** — how the assessment's 2017 claim was reduced to v9's actual
   CBC/JNI scheme, and what an audit independently confirmed.
   `docs/notes/bravehaxvius_assessment.md` §2 gives it **exactly**, quoted from the 2017 clientless implementation's
   own code (`BraveHaxvius/Crypto.cs:20-23`): **AES-ECB, PKCS7, base64; key = the 8-char token's UTF-8 bytes
   zero-filled to 16; one key per request (`request.EncodeKey`) used for BOTH directions.** It also notes that the
   network and MST paths share that key form — **independently confirmed here the same day** by decrypting a real
   shipped MST `.dat` with the manifest-derived key (§2.3(c3) above). And §8 of the assessment *measures* the
   transfer: in v9.0.0 the per-request `RequestID`/`EncodeKey` pool is **131/135 = 97 %** present, the envelope field
   names **10/10**, the route tokens 132/133. **Residual, and it is the real work:** the *payload trees* are
   explicitly **not** transferable (only the token vocabulary is), the `SignalKey`'s exact role in signing is still
   open, and the AES-ECB/PKCS7 scheme should be **read in v9.0.0's own crypto functions** rather than assumed from a
   2017 client. So this is no longer the blocker it was recorded as — **item 1 is the proof step**, and the
   assessment's §6 (certificate pinning on Android) is required reading before probing with a real client.

   **Verified against v9.0.0's own code the same day — and it corrects the MODE.** The request body is encrypted by a
   **JNI call into Java**, not natively: `CommonUtils::encodeCStringForBase64WithNewCrypto` (`0x1e31f14` → the real
   body at `0x1f3cba0`) is a `cocos2d::JniHelper::getStaticMethodInfo` bridge to
   `com/square_enix/android_googleplay/FFBEWW/LapisJNI.encodeCStringForBase64WithNewCrypto(String,String,String)`,
   and it passes the literal **`dZMjkk8gFDzKHlsx`** — *the same 16-byte IV the MST path already uses*
   (`scripts/mst_decode.py:80`: `CBC_IV = b"dZMjkk8gFDzKHlsx"`, "16-byte ASCII string, single hit in .rodata").
   Three consequences: (a) the network cipher lives in **Java**, which is why `libgame.so`'s statically linked OpenSSL
   (`AES_ecb_encrypt`, `AES_set_encrypt_key`, `base64Encode/Decode`) has **no native caller on that path** — a scan of
   all 832,943 `bl` instructions in `.text` found none; (b) **the mode is CBC, not the ECB the 2017 client used** —
   and it is now *settled at the Java level*, not inferred: jadx of the APK's own `classes.dex` gives
   `LapisJNI.NEW_CRYPTO_ALGORITHM = "AES/CBC/PKCS5Padding"`, a manual `paddingWithPKCSX(str, 16)` before
   `Cipher.getInstance(...)` / `cipher.init(1, secretKeySpec, ivParameterSpec)`, and the **key form the assessment
   claimed**: `byte[] bArr = new byte[16]; System.arraycopy(key.getBytes("UTF-8"), 0, bArr, 0, min(16, len))` —
   8 chars + NULs. (Independently re-derived by an audit agent, 2026-09-16, which also confirmed the APK's
   `libgame.so` is byte-identical to ours, sha256 `a1276540…6937e4`.) **Correction to an earlier line of this
   note:** the MST path is *not* uniformly "CBC, different from the network" — it is **per-table** (157 CBC / 62 ECB,
   `mst_decoded/_DECODE_REPORT.tsv`, and `docs/archive/AES_KEY_SEARCH.md`) and the **network path is also CBC with
   this same IV**, so the IV is *shared*, not path-specific; the thing that does not transfer is the assessment's
   **ECB mode**. (c) the crypto lives behind `LapisJNI`, so `libgame.so`'s OpenSSL AES is a dead end for this path —
   `Crypto::aesEcb` (`0x19b64dc`) exists natively with **0 direct/PLT callers** (residual: a GOT `ldr`+`blr` would
   evade that scan). **Response direction, verified:** the same virtual slot `[vptr+0x28]` supplies the key on both
   sides — `MissionStartRequest::getEncodeKey` (`0x19a8f10`) is the concrete override, used for the request in
   `BaseRequest::getSendData` (`0x198f0a4`) and for the response in `GameResponseParser::parseResponseAsync`
   (`0x190f318`).
3. **Version negotiation + CDN routing** — the `Ver<N>_<token>.dat` assignment rule, the mandatory boot
   table set (`LoginScene::MIN_MST_FILE`), and the CDN host via `EnvUrlMst` / `P_GS_CDN_PATH`. The files to
   serve are the ones `docs/COVERAGE_LEDGER.md` already counts.
4. **Then the areas in order** — battle (§2.5, the most pre-documented), gacha, shop, friends/events.

**Closed since this plan was written.** §6 **items 1–2 are settled** — the `UserInfo` field map (item 1) and
  the crypto envelope (§1e), the latter **confirmed against a live client** on 2026-09-17 — and the boot packet
  is identified (item 1's sharpened question, above). The synthetic-server effort now has its own workstream
  home, `APK_emulator_work/` (curation `D11` closed), whose `docs/HARNESS_PLAN.md` §7 records the first live
  run: Waydroid + `libndk`, system-store CA, host redirect, APK launching and its boot request decrypted.
  What remains of the plan is **item 3** (version negotiation + CDN routing) and **item 4** (the areas in
  order).
