// YourAnalyst desktop app (JXA, compiled by build_app.sh into a stay-open applet).
// A native window with a WKWebView on the dashboard (http://localhost:3500). On launch it
// starts whatever is not running yet: Docker (Postgres, n8n, analysis API), the web
// server and the LM Studio server, while a loading screen is shown. While open, a watchdog
// restarts the web server if it stops.
//
// JXA cannot call Objective-C blocks, so delegate methods with a completionHandler
// (JS confirm/alert, file pickers, navigation policy) are left unimplemented: WebKit then
// uses its defaults (confirm() returns false). Downloads go through a script message instead.
ObjC.import('Cocoa');
ObjC.import('WebKit');

const PROJECT = '/Users/canoktay/Desktop/no code n8n/YourAnalyst';
const APP_URL = 'http://localhost:3500/';
const PATH    = 'PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin';
const app     = Application.currentApplication();
app.includeStandardAdditions = true;

let win, web, uiDelegate, winDelegate, msgHandler;   // kept global so they are not released

const sh = cmd => { try { return app.doShellScript(`${PATH}; ${cmd}`); } catch (e) { return ''; } };
const q  = s => `'${String(s).replace(/'/g, `'\\''`)}'`;
const up = url => !['', '000'].includes(sh(`curl -s -o /dev/null -w '%{http_code}' --max-time 2 ${q(url)}`));
const pump = s => $.NSRunLoop.currentRunLoop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(s));

// target=_blank links and window.open (Yahoo, economic calendar news) open in the default browser.
ObjC.registerSubclass({
    name: 'YAUIDelegate', superclass: 'NSObject',
    methods: {
        'webView:createWebViewWithConfiguration:forNavigationAction:windowFeatures:': {
            types: ['id', ['id', 'id', 'id', 'id']],
            implementation: (wv, cfg, action, features) => {
                const url = action.request.URL;
                if (!url.isNil()) $.NSWorkspace.sharedWorkspace.openURL(url);
                return $();
            },
        },
    },
});

// Watchdog: if the web server stops while the app is open (it may have been started by
// another process that exited), start it again and reload the page.
let restarting = false;
ObjC.registerSubclass({
    name: 'YAWatchdog', superclass: 'NSObject',
    methods: {
        'tick:': {
            types: ['void', ['id']],
            implementation: () => {
                if (restarting || up(APP_URL)) return;
                restarting = true;
                sh(`cd ${q(PROJECT)} && nohup python3 web/server.py >/tmp/youranalyst-web.log 2>&1 &`);
                for (let i = 0; i < 20 && !up(APP_URL); i++) pump(0.5);
                if (up(APP_URL)) web.loadRequest($.NSURLRequest.requestWithURL($.NSURL.URLWithString(APP_URL)));
                restarting = false;
            },
        },
    },
});
let watchdog, watchdogTimer;

// Closing the window quits the app.
ObjC.registerSubclass({
    name: 'YAWindowDelegate', superclass: 'NSObject',
    methods: {
        'windowWillClose:': { types: ['void', ['id']], implementation: () => $.NSApp.terminate($()) },
    },
});

// Downloads (Export JSON, Download PDF): the injected script posts {name, data: base64}.
ObjC.registerSubclass({
    name: 'YADownloadHandler', superclass: 'NSObject',
    methods: {
        'userContentController:didReceiveScriptMessage:': {
            types: ['void', ['id', 'id']],
            implementation: (ucc, message) => {
                const body = ObjC.deepUnwrap(message.body) || {};
                const name = String(body.name || 'download').replace(/[\/:]/g, '-');
                const data = $.NSData.alloc.initWithBase64EncodedStringOptions(body.data || '', 0);
                const dir  = $.NSHomeDirectory().js + '/Downloads/';
                let path = dir + name, i = 1;
                const dot = name.lastIndexOf('.');
                while ($.NSFileManager.defaultManager.fileExistsAtPath(path)) {
                    path = dir + (dot > 0 ? `${name.slice(0, dot)} (${i})${name.slice(dot)}` : `${name} (${i})`);
                    i++;
                }
                if (data.writeToFileAtomically(path, true)) {
                    $.NSWorkspace.sharedWorkspace.activateFileViewerSelectingURLs($.NSArray.arrayWithObject($.NSURL.fileURLWithPath(path)));
                }
            },
        },
    },
});

// Turns <a download href="blob:/data:"> clicks into a message to the app.
const DOWNLOAD_JS = `(() => {
  const send = a => {
    const name = a.getAttribute('download') || 'download';
    fetch(a.href).then(r => r.blob()).then(b => {
      const fr = new FileReader();
      fr.onload = () => window.webkit.messageHandlers.download.postMessage({ name, data: String(fr.result).split(',')[1] || '' });
      fr.readAsDataURL(b);
    });
  };
  const isDl = a => a instanceof HTMLAnchorElement && a.hasAttribute('download') && /^(blob|data):/.test(a.href);
  const click = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function () { if (isDl(this)) return send(this); return click.call(this); };
  const dispatch = EventTarget.prototype.dispatchEvent;
  EventTarget.prototype.dispatchEvent = function (ev) {
    if (ev && ev.type === 'click' && isDl(this)) { send(this); return false; }
    return dispatch.call(this, ev);
  };
  document.addEventListener('click', ev => {
    const a = ev.target.closest && ev.target.closest('a');
    if (a && isDl(a)) { ev.preventDefault(); send(a); }
  }, true);
})();`;

function page(title, text) {
    return `<html><body style="margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;
        background:#080808;color:#e8e6e1;font:15px -apple-system,Helvetica,sans-serif;gap:10px">
        <div style="font-size:26px;font-weight:600;letter-spacing:-0.5px">${title}</div>
        <div style="color:#8a877f">${text}</div></body></html>`;
}
const show = (title, text) => { web.loadHTMLStringBaseURL(page(title, text), $()); pump(0.05); };

function buildWindow() {
    $.NSApplication.sharedApplication.setActivationPolicy($.NSApplicationActivationPolicyRegular);

    const cfg = $.WKWebViewConfiguration.alloc.init;
    msgHandler = $.YADownloadHandler.alloc.init;
    cfg.userContentController.addScriptMessageHandlerName(msgHandler, 'download');
    cfg.userContentController.addUserScript($.WKUserScript.alloc.initWithSourceInjectionTimeForMainFrameOnly(
        DOWNLOAD_JS, $.WKUserScriptInjectionTimeAtDocumentEnd, true));

    const screen = $.NSScreen.mainScreen.visibleFrame;
    const w = Math.min(1600, screen.size.width - 40), h = Math.min(1000, screen.size.height - 40);
    const style = $.NSWindowStyleMaskTitled | $.NSWindowStyleMaskClosable
                | $.NSWindowStyleMaskMiniaturizable | $.NSWindowStyleMaskResizable;
    win = $.NSWindow.alloc.initWithContentRectStyleMaskBackingDefer($.NSMakeRect(0, 0, w, h), style, $.NSBackingStoreBuffered, false);
    win.title = 'YourAnalyst';
    win.appearance = $.NSAppearance.appearanceNamed('NSAppearanceNameDarkAqua');
    win.backgroundColor = $.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.03, 0.03, 0.03, 1);
    win.minSize = $.NSMakeSize(1024, 680);
    win.releasedWhenClosed = false;
    win.center;
    win.setFrameAutosaveName('YourAnalystMain');   // remembers size and position
    winDelegate = $.YAWindowDelegate.alloc.init;
    win.delegate = winDelegate;

    web = $.WKWebView.alloc.initWithFrameConfiguration($.NSMakeRect(0, 0, w, h), cfg);
    web.autoresizingMask = $.NSViewWidthSizable | $.NSViewHeightSizable;
    web.setValueForKey(false, 'drawsBackground');
    uiDelegate = $.YAUIDelegate.alloc.init;
    web.UIDelegate = uiDelegate;
    win.contentView = web;

    // View › Reload (⌘R): reload: goes to the web view through the responder chain.
    const viewMenu = $.NSMenu.alloc.initWithTitle('View');
    const reload = $.NSMenuItem.alloc.initWithTitleActionKeyEquivalent('Reload', 'reload:', 'r');
    reload.target = web;
    viewMenu.addItem(reload);
    const viewItem = $.NSMenuItem.alloc.initWithTitleActionKeyEquivalent('View', $(), '');
    viewItem.submenu = viewMenu;
    if (!$.NSApp.mainMenu.isNil()) $.NSApp.mainMenu.addItem(viewItem);

    win.makeKeyAndOrderFront($());
    win.makeFirstResponder(web);
    $.NSApp.activateIgnoringOtherApps(true);
}

// Starts what is missing, showing progress, then waits for the dashboard.
function startServices() {
    if (!up('http://localhost:5678/healthz')) {
        if (sh('docker info >/dev/null 2>&1 && echo ok') !== 'ok') {
            show('Starting Docker…', 'Postgres, n8n and the analysis API run in Docker.');
            sh('open -ga Docker');
            for (let i = 0; i < 120 && sh('docker info >/dev/null 2>&1 && echo ok') !== 'ok'; i++) pump(1);
        }
        show('Starting services…', 'Postgres, n8n and the analysis API');
        sh(`cd ${q(PROJECT)} && docker compose up -d >/dev/null 2>&1`);
    }
    if (!up('http://localhost:1234/v1/models')) {
        show('Starting the AI model…', 'LM Studio local server');
        sh(`[ -x ~/.lmstudio/bin/lms ] && ~/.lmstudio/bin/lms server start >/dev/null 2>&1`);
    }
    if (!up(APP_URL)) {
        show('Starting YourAnalyst…', 'Web server on port 3500');
        sh(`cd ${q(PROJECT)} && nohup python3 web/server.py >/tmp/youranalyst-web.log 2>&1 &`);
    }
    for (let i = 0; i < 60 && !up(APP_URL); i++) pump(1);
    if (!up(APP_URL)) {
        show('YourAnalyst could not start', 'See /tmp/youranalyst-web.log');
        return false;
    }
    for (let i = 0; i < 90 && !up('http://localhost:5678/healthz'); i++) {
        if (i === 0) show('Waiting for n8n…', 'The workflows are loading.');
        pump(1);
    }
    return true;
}

function run() {
    buildWindow();
    show('YourAnalyst', 'Starting…');
    if (startServices()) web.loadRequest($.NSURLRequest.requestWithURL($.NSURL.URLWithString(APP_URL)));
    watchdog = $.YAWatchdog.alloc.init;
    watchdogTimer = $.NSTimer.scheduledTimerWithTimeIntervalTargetSelectorUserInfoRepeats(15, watchdog, 'tick:', $(), true);
}

// Dock icon clicked while the window is hidden or minimised.
function reopen() {
    if (win) { win.deminiaturize($()); win.makeKeyAndOrderFront($()); }
}
