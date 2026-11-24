// Shared constants. Attempt ids must match verify/evaluate.py ATTEMPTS exactly.
#pragma once
inline constexpr wchar_t kAcName[]    = L"HotelHR.WV2Box";       // contained arm profile, ZERO capabilities
inline constexpr wchar_t kAcCtlName[] = L"HotelHR.WV2BoxCtl";    // control arm B profile: internetClient + privateNetworkClientServer, TEST ONLY
inline constexpr wchar_t kVHost[]     = L"app.hotelhr.invalid";  // .invalid never resolves; only the handler serves it
inline constexpr const wchar_t* kAttemptIds[] = {L"host_tcp_loopback", L"host_tcp_remote", L"rend_http_loopback", L"rend_http_remote", L"rend_ws_remote"};
inline constexpr unsigned short kLoopPort = 47631;               // launcher-owned listener on 127.0.0.1 (outside the box)
