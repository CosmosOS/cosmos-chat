// #/register is Element's client-side route. The homeserver refuses all raw
// registration, so anyone landing there goes to the captcha-gated signup.
(function () {
    function toJoin() {
        if (location.hash.indexOf("#/register") === 0) location.replace("/join");
    }
    window.addEventListener("hashchange", toJoin);
    toJoin();
})();
