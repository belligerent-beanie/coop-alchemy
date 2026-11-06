// Phones are not supported (the server also redirects them; this catches desktop-mode browsers).
if (/iPhone|iPod|Android.+Mobile|Mobi/i.test(navigator.userAgent) ||
    (matchMedia("(pointer: coarse)").matches && Math.min(screen.width, screen.height) < 600)) {
  location.replace("/laptop");
}
