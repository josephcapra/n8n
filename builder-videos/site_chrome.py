#!/usr/bin/env python3
"""Site chrome for the finder subdomain: tracking tags + floating call / consultation / chat widget.

These pages live on search.paradiserealtyfla.com, outside RealGeeks, so nothing RealGeeks injects into
www reaches them. Without this module the subdomain records ZERO sessions in GA4, Google Ads and Meta,
and shows none of the contact affordances the rest of the site has.

CHROME is lifted verbatim from the live site's own snippet
(~/paradise-realty-beta/realgeeks-snippets/floating-footer-v5.2.html) so behaviour matches exactly and
there is one source of truth to re-copy from when Joe edits it in RealGeeks. Sections taken: the CSS,
the floating action row, the consultation modal and its logic, the chat container and toggle, the Meta
Pixel, and Google Ads + GTM - including the ?agent_id= attribution that puts the sharing agent's phone
on the call button.

DELIBERATELY NOT taken: the v5.2 SEO structured-data / canonical injection block. It exists because
RealGeeks templates deny access to <head> on dynamic area pages; these pages are ours and already emit
their own canonical and JSON-LD, so running it here would fight tags we set correctly.

GA4 is not in that snippet - RealGeeks loads it in the www <head> - so GA4_HEAD below reproduces the
property and Ads IDs the live site configures. GA4's default cookie domain is the registrable domain,
so a visitor moving between www and search stays in one session without extra cross-domain config.
"""

# Loaded in <head>, matching www: GA4 property, the second GA4 stream, and the Google Ads account.
GA4_HEAD = """<script async src="https://www.googletagmanager.com/gtag/js?id=G-G6YVB7Y1Q5"></script>
<script>
window.dataLayer = window.dataLayer || [];
function gtag(){dataLayer.push(arguments);}
gtag('js', new Date());
gtag('config', 'G-G6YVB7Y1Q5');
gtag('config', 'G-SGTZV17V8T');
gtag('config', 'AW-718023737');
</script>"""

# Everything else goes at the end of <body>.
CHROME = r"""
<style>
      /* ============ MODAL (Book a PRIVATE Consultation) ============ */
      .prf-overlay {
        display: none;
        position: fixed;
        inset: 0;
        background: rgba(0,0,0,0.65);
        z-index: 999999;
        align-items: center;
        justify-content: center;
        padding: 16px 16px 80px;
      }

      .prf-overlay.prf-open { display: flex !important; }

      .prf-modal {
        background: white;
        border-radius: 16px;
        width: 100%;
        max-width: 600px;
        height: 88vh;
        max-height: 88vh;
        overflow: hidden;
        position: relative;
        box-shadow: 0 24px 80px rgba(0,0,0,0.4);
        display: flex;
        flex-direction: column;
      }

      .prf-modal-header {
        background: #0f2744;
        padding: 14px 20px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        flex-shrink: 0;
        border-bottom: 2px solid #c9a84c;
      }

      .prf-modal-title {
        color: rgba(255,255,255,0.8);
        font-size: 13px;
        font-style: italic;
        font-family: Georgia, serif;
      }

      .prf-modal-close {
        background: rgba(255,255,255,0.15);
        border: 1px solid rgba(255,255,255,0.25);
        color: white;
        width: 30px;
        height: 30px;
        border-radius: 50%;
        cursor: pointer;
        font-size: 16px;
        display: flex;
        align-items: center;
        justify-content: center;
        font-family: Arial, sans-serif;
        flex-shrink: 0;
      }

      .prf-modal-close:hover { background: rgba(255,255,255,0.3); }

      .prf-iframe-wrap {
        flex: 1;
        overflow: hidden;
        -webkit-overflow-scrolling: touch;
        min-height: 0;
      }

      .prf-iframe-wrap iframe {
        width: 100%;
        height: 100%;
        border: none;
        display: block;
      }

      @media (max-width: 640px) {
        .prf-overlay { padding: 8px 8px 70px; }
        .prf-modal { max-height: 92vh; height: 92vh; border-radius: 12px; }
      }

      /* ============ FLOATING ACTION ROW (evenly spaced bottom row) ============ */
      #prfFloatRow {
        position: fixed;
        bottom: 20px;
        left: 0;
        right: 0;
        z-index: 99990;
        display: flex;
        justify-content: space-evenly;
        align-items: center;
        gap: 12px;
        padding: 0 20px;
        pointer-events: none; /* let clicks pass through gaps to the page */
      }
      #prfFloatRow > * {
        pointer-events: auto; /* but the buttons themselves are clickable */
      }

      /* ============ CONSULTATION BUTTON ============ */
      #prfOpenBtn {
        background: #c9a84c;
        color: #0f2744;
        border: none;
        padding: 14px 32px;
        border-radius: 30px;
        font-family: Arial, sans-serif;
        font-size: 13px;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        cursor: pointer;
        white-space: nowrap;
        box-shadow: 0 4px 14px rgba(0,0,0,0.28);
        transition: background 0.2s, transform 0.2s, box-shadow 0.2s;
      }

      #prfOpenBtn:hover {
        background: #e8c460;
        transform: translateY(-2px);
        box-shadow: 0 6px 20px rgba(201,168,76,0.45);
      }

      /* ============ CALL BUTTON ============ */
      .floating-contact-btn {
        background: #00796b;
        color: #fff;
        padding: 13px 23px;
        font-weight: bold;
        border-radius: 30px;
        box-shadow: 0 4px 8px rgba(0,0,0,.3);
        text-decoration: none;
        text-align: center;
        transition: background-color .3s ease;
        white-space: nowrap;

        opacity: 0.6;
      }
      .floating-contact-btn:hover { background: #005f53;
        opacity: 1;
      }
      .btn-phone { font-size: 16px; font-weight: bold; }

      /* ============ CHAT BUTTON ============ */
      #prf-chat-btn {
        width: 58px;
        height: 58px;
        border-radius: 50%;
        background: #00796b;
        color: #fff;
        border: none;
        cursor: pointer;
        box-shadow: 0 6px 18px rgba(0,0,0,0.28);
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 26px;
        flex-shrink: 0;
        transition: transform 0.2s ease, background 0.3s ease;

        opacity: 0.6;
      }
      #prf-chat-btn:hover {
        background: #005f53;
        transform: scale(1.06);

        opacity: 1;
      }

      #prf-chat-container {
        position: fixed;
        bottom: 90px;
        right: 20px;
        width: 340px;
        height: 510px;
        background: #fff;
        border-radius: 14px;
        box-shadow: 0 10px 28px rgba(0,0,0,0.32);
        z-index: 999999;
        overflow: hidden;
        display: none;
        opacity: 0;
        transform: translateY(12px);
        transition: opacity 0.25s ease, transform 0.25s ease;
      }
      #prf-chat-container.visible {
        display: block;
        opacity: 1;
        transform: translateY(0px);
      }

      #prf-chat-header {
        background: #00796b;
        color: #fff;
        padding: 10px 12px;
        font-weight: bold;
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-family: Arial, sans-serif;
        font-size: 14px;
      }

      #prf-chat-close {
        background: none;
        border: none;
        color: #fff;
        font-size: 20px;
        cursor: pointer;
        line-height: 1;
      }

      #prf-chat-frame {
        width: 100%;
        height: calc(100% - 40px);
        border: none;
      }

      /* ============ MOBILE: tighten the row ============ */
      @media (max-width: 640px) {
        #prfFloatRow {
          padding: 0 10px;
          gap: 8px;
        }

        .floating-contact-btn {
          padding: 10px 14px;
        }
        .btn-phone { font-size: 13px; }

        #prfOpenBtn {
          padding: 10px 14px;
          font-size: 11px;
          letter-spacing: 0.04em;
        }

        #prf-chat-btn {
          width: 50px;
          height: 50px;
          font-size: 22px;
        }

        #prf-chat-container {
          width: 88vw;
          height: 70vh;
          right: 4vw;
          bottom: 80px;
        }
      }
    </style>

<!-- ============ FLOATING ACTION ROW (phone, consultation, chat) ============ -->
<div id="prfFloatRow"><a id="floatingCallBtn" class="floating-contact-btn" href="tel:7722477110"> <span class="btn-phone" id="floatingCallPhoneText">(772) 247-7110</span> </a> <button id="prfOpenBtn" type="button">Book a PRIVATE Consultation</button> <button id="prf-chat-btn" aria-label="Open chat">&#128172;</button></div>
<!-- CONSULTATION MODAL -->
<div id="prfOverlay" class="prf-overlay">
<div class="prf-modal">
<div class="prf-modal-header"><span class="prf-modal-title">Paradise Realty FLA &mdash; Free Consultation</span> <button id="prfCloseBtn" class="prf-modal-close">&times;</button></div>
<div class="prf-iframe-wrap"><iframe id="prfIframe" src="about:blank" title="Book a Free Consultation with Paradise Realty FLA" sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"></iframe></div>
</div>
</div>
<!-- CHAT CONTAINER -->
<div id="prf-chat-container">
<div id="prf-chat-header"><span>Chat with Us</span> <button id="prf-chat-close" aria-label="Close chat">&times;</button></div>
<iframe id="prf-chat-frame" src="https://joegpt-383923649216.us-east1.run.app/chat-ui" allow="clipboard-write *" sandbox="allow-scripts allow-same-origin allow-forms allow-popups"></iframe></div>

<!-- =========================================================
         CONSULTATION MODAL LOGIC
         ========================================================= -->

<script>
    (function() {
      var FORM_URL    = 'https://paradise-api-383923649216.us-east1.run.app/form';
      var iframeLoaded = false;

      var overlay  = document.getElementById('prfOverlay');
      var iframe   = document.getElementById('prfIframe');
      var openBtn  = document.getElementById('prfOpenBtn');
      var closeBtn = document.getElementById('prfCloseBtn');

      function openModal() {
        if (!iframeLoaded) {
          iframe.src = FORM_URL;
          iframeLoaded = true;
        }
        overlay.classList.add('prf-open');
        document.body.style.overflow = 'hidden';
      }

      function closeModal() {
        overlay.classList.remove('prf-open');
        document.body.style.overflow = '';
      }

      openBtn.addEventListener('click', function(e) { e.stopPropagation(); openModal(); });
      closeBtn.addEventListener('click', function(e) { e.stopPropagation(); closeModal(); });

      overlay.addEventListener('click', function(e) {
        if (e.target === overlay) closeModal();
      });

      document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && overlay.classList.contains('prf-open')) closeModal();
      });
    })();
    </script>

<!-- =========================================================
         META PIXEL
         ========================================================= -->

<script>
    !function(f,b,e,v,n,t,s){if(f.fbq)return;n=f.fbq=function(){n.callMethod?
    n.callMethod.apply(n,arguments):n.queue.push(arguments)};if(!f._fbq)f._fbq=n;
    n.push=n;n.loaded=!0;n.version='2.0';n.queue=[];t=b.createElement(e);t.async=!0;
    t.src=v;s=b.getElementsByTagName(e)[0];s.parentNode.insertBefore(t,s)}
    (window,document,'script','https://connect.facebook.net/en_US/fbevents.js');
    fbq('init','3038838742803237');fbq('track','PageView');
    </script>

<!-- =========================================================
         GOOGLE ADS + GTM
         ========================================================= -->

<script async="" src="https://www.googletagmanager.com/gtag/js?id=AW-718023737"></script>
<script>
    window.dataLayer=window.dataLayer||[];
    function gtag(){dataLayer.push(arguments)}
    gtag('js',new Date());
    gtag('config','AW-718023737');
    </script>
<script>
    (function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start':new Date().getTime(),event:'gtm.js'});
    var f=d.getElementsByTagName(s)[0],j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';
    j.async=true;
    j.src='https://www.googletagmanager.com/gtm.js?id='+i+dl;
    f.parentNode.insertBefore(j,f);
    })(window,document,'script','dataLayer','GTM-PC4CRC9K');
    </script>

<!-- =========================================================
         CHAT TOGGLE + AGENT ATTRIBUTION
         ========================================================= -->

<script>
      /* ===== Chat Toggle ===== */
      const chatBtn = document.getElementById("prf-chat-btn");
      const chatContainer = document.getElementById("prf-chat-container");
      const chatClose = document.getElementById("prf-chat-close");

      function openChat() { chatContainer.classList.add("visible"); }
      function closeChat() { chatContainer.classList.remove("visible"); }

      chatBtn.addEventListener("click", () => {
        if (chatContainer.classList.contains("visible")) closeChat();
        else openChat();
      });
      chatClose.addEventListener("click", closeChat);

      /* =========================================================
         Agent Attribution  (v5 — agent_id aware)
         ---------------------------------------------------------
         Agents share RealGeeks links tagged with ?agent_id=NNNN
         (the same param that routes the CRM lead to that agent).
         This makes the floating Call button show THAT agent's phone.

         Resolution order — NEWEST tagged link wins:
           1. ?agent_id= in the CURRENT url   -> that agent (refresh cookie)
           2. agent slug in the URL path      -> that agent (refresh cookie)  [legacy links]
           3. our saved attribution cookie    -> keep showing that agent
           4. RealGeeks landing_page_agent_id -> that agent  [first-touch fallback]
           5. otherwise                       -> office number
         ========================================================= */

      const PR_AGENT_COOKIE = "pr_agent_phone";
      const PR_AGENT_COOKIE_DAYS = 30;
      const officePhoneDigits = "7722477110";

      /* RealGeeks agent_id -> phone. ADD NEW AGENTS HERE. */
      const agentPhonesById = {
        "141470": "7722477110", // Joe Capra
        "152959": "7722477110", // Joseph Capra (broker/owner)
        "145598": "9045660550", // Caydee Ward
        "142941": "7722401768", // Christine Menendez
        "148116": "5612229473", // Kerry O'Donoghue
        "149089": "9787017345", // Karen Griffin
        "149349": "9787017345", // Karen E. Griffin (2nd record)
        "153148": "5614279400", // Kristen Coughanour
        "153514": "7722152280", // Melissa Corbett
        "142600": "7724855647", // Sandy Vukic
        "142485": "7147492393"  // Jenny Starts
      };

      /* Legacy: agent first-name slug IN THE PATH -> phone */
      const agentPhonesBySlug = {
        "dale":      "9546383224",
        "jenny":     "7147492393",
        "gloria":    "7725954480",
        "bryce":     "5617580888",
        "christine": "7722401768",
        "sandy":     "7724855647",
        "caydee":    "9045660550",
        "joe":       "7722477110",
        "kerry":     "5612229473",
        "karen":     "9787017345",
        "kristen":   "5614279400",
        "melissa":   "7722152280"
      };

      function digitsOnly(num){ return String(num || "").replace(/[^\d]/g,""); }

      function formatPhone(num){
        num = digitsOnly(num);
        return num.length === 10
          ? `(${num.substr(0,3)}) ${num.substr(3,3)}-${num.substr(6,4)}`
          : num;
      }

      function getCookie(name){
        const parts = document.cookie.split(";").map(p => p.trim());
        for(const p of parts){
          if(p.startsWith(name + "=")) return decodeURIComponent(p.substring(name.length + 1));
        }
        return "";
      }

      function setCookie(name, value, days){
        const v = encodeURIComponent(value);
        const maxAge = days ? `; max-age=${days*24*60*60}` : "";
        document.cookie = `${name}=${v}${maxAge}; path=/; samesite=lax`;
      }

      function getQueryAgentId(){
        try {
          const id = new URLSearchParams(window.location.search).get("agent_id");
          return id ? digitsOnly(id) : "";
        } catch(_) { return ""; }
      }

      function findAgentSlugInPath(){
        const segments = window.location.pathname
          .split("/")
          .map(s => s.toLowerCase())
          .filter(Boolean);
        for(const seg of segments){
          if(agentPhonesBySlug[seg]) return seg;
        }
        return "";
      }

      function resolveAgentPhone(){
        const qid = getQueryAgentId();
        if(qid && agentPhonesById[qid]) return { phone: digitsOnly(agentPhonesById[qid]), persist: true };

        const slug = findAgentSlugInPath();
        if(slug && agentPhonesBySlug[slug]) return { phone: digitsOnly(agentPhonesBySlug[slug]), persist: true };

        const cookiePhone = digitsOnly(getCookie(PR_AGENT_COOKIE));
        if(cookiePhone.length === 10) return { phone: cookiePhone, persist: false };

        const rgId = digitsOnly(getCookie("landing_page_agent_id"));
        if(rgId && agentPhonesById[rgId]) return { phone: digitsOnly(agentPhonesById[rgId]), persist: true };

        return { phone: officePhoneDigits, persist: false };
      }

      function updateFloatingCallButton(){
        const btn = document.getElementById("floatingCallBtn");
        const txt = document.getElementById("floatingCallPhoneText");
        if(!btn || !txt) return;

        const { phone, persist } = resolveAgentPhone();
        const digits = digitsOnly(phone);
        if(digits.length !== 10) return;

        if(persist){
          setCookie(PR_AGENT_COOKIE, digits, PR_AGENT_COOKIE_DAYS);
        }

        btn.href = "tel:" + digits;
        txt.textContent = formatPhone(digits);
      }

      updateFloatingCallButton();
    </script>
"""
