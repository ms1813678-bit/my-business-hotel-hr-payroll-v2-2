// Unique ownership, no Windows headers (so it can be compiled and run on any host: see test/raii_test.cpp).
// Move transfers the resource and zeroes the source. Copy is blocked at compile time. Close() is checked:
// it zeroes the owner BEFORE calling the closer (so a failing or repeated close can never double-free) and reports failure.
#pragma once
#include <utility>
#include <type_traits>
inline void (*g_closeFailHook)(const char* api, long code) = nullptr;   // set once by the program; destructors report through it

// Tr must provide: using type; static type null(); static type norm(type); static bool close(type); static long err(); static const char* name;
template<class Tr> class Unique {
  typename Tr::type v_ = Tr::null();
public:
  using type = typename Tr::type;
  Unique() = default;
  explicit Unique(type x) : v_(Tr::norm(x)) {}
  Unique(const Unique&) = delete;
  Unique& operator=(const Unique&) = delete;
  Unique(Unique&& o) noexcept : v_(o.v_) { o.v_ = Tr::null(); }
  Unique& operator=(Unique&& o) noexcept { if (this != &o) { Close(); v_ = o.v_; o.v_ = Tr::null(); } return *this; }
  ~Unique() { Close(); }
  type get() const { return v_; }
  explicit operator bool() const { return v_ != Tr::null(); }
  type* put() { Close(); return &v_; }            // for out-parameters: closes any old value first
  bool Close() {                                   // true if nothing to close or the close succeeded
    if (v_ == Tr::null()) return true;
    type x = v_; v_ = Tr::null();
    if (Tr::close(x)) return true;
    if (g_closeFailHook) g_closeFailHook(Tr::name, Tr::err());
    return false; }
};
