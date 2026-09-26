package com.pencilfinely.expmonitor

import android.annotation.SuppressLint
import android.app.Activity
import android.graphics.Color
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.view.View
import android.view.ViewGroup
import android.view.WindowInsets
import android.webkit.*
import android.widget.*
import java.net.URI

/** A platform container only. No experiment state, credentials or commands cross a JS bridge. */
class MainActivity : Activity() {
    private var browser: WebView? = null
    private var origin = ""
    private val green = Color.rgb(35, 79, 65)
    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) window.setDecorFitsSystemWindows(false)
        showConnection()
    }

    private fun layout(): LinearLayout {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.rgb(244, 246, 241))
        }
        root.setOnApplyWindowInsetsListener { view, insets ->
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                val safe = insets.getInsets(WindowInsets.Type.systemBars() or
                    WindowInsets.Type.displayCutout() or WindowInsets.Type.ime())
                view.setPadding(safe.left, safe.top, safe.right, safe.bottom)
                WindowInsets.CONSUMED
            } else {
                applyLegacyInsets(view, insets)
            }
        }
        setContentView(root)
        root.requestApplyInsets()
        return root
    }

    @Suppress("DEPRECATION")
    private fun applyLegacyInsets(view: View, insets: WindowInsets): WindowInsets {
        val cutout = insets.displayCutout
        view.setPadding(maxOf(insets.systemWindowInsetLeft, cutout?.safeInsetLeft ?: 0),
            maxOf(insets.systemWindowInsetTop, cutout?.safeInsetTop ?: 0),
            maxOf(insets.systemWindowInsetRight, cutout?.safeInsetRight ?: 0),
            maxOf(insets.systemWindowInsetBottom, cutout?.safeInsetBottom ?: 0))
        return insets.consumeSystemWindowInsets().consumeDisplayCutout()
    }

    private fun showConnection() {
        browser?.let { (it.parent as? ViewGroup)?.removeView(it); it.stopLoading(); it.destroy() }
        browser = null
        val root = layout()
        val scroll = ScrollView(this)
        val form = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(24), dp(32), dp(24), dp(24)) }
        scroll.addView(form); root.addView(scroll)
        form.addView(TextView(this).apply { setText(R.string.connection_title); textSize = 25f; setTextColor(green) })
        form.addView(TextView(this).apply { setText(R.string.connection_hint); setPadding(0, dp(18), 0, dp(18)) })
        val address = EditText(this).apply {
            setHint(R.string.address_hint)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setSingleLine(true)
            setText(getSharedPreferences("connection", MODE_PRIVATE).getString("origin", ""))
        }
        val allowHttp = CheckBox(this).apply { setText(R.string.allow_private_http) }
        val error = TextView(this).apply { setTextColor(Color.rgb(156, 58, 52)); setPadding(0, dp(10), 0, dp(10)) }
        form.addView(address); form.addView(allowHttp); form.addView(error)
        form.addView(Button(this).apply {
            setText(R.string.connect)
            setOnClickListener {
                try {
                    val uri = URI(address.text.toString().trim())
                    val scheme = uri.scheme?.lowercase()
                    require((scheme == "http" || scheme == "https") && uri.host != null &&
                        uri.rawUserInfo == null && uri.rawQuery == null && uri.rawFragment == null &&
                        (uri.path.isNullOrEmpty() || uri.path == "/") &&
                        (uri.port == -1 || uri.port in 1..65535)) { getString(R.string.invalid_address) }
                    if (scheme == "http") require(allowHttp.isChecked && privateHost(uri.host)) {
                        getString(R.string.http_not_allowed)
                    }
                    origin = URI(scheme, null, uri.host.lowercase(), uri.port, null, null, null).toString()
                    getSharedPreferences("connection", MODE_PRIVATE).edit().putString("origin", origin).apply()
                    openMonitor()
                } catch (failure: Exception) { error.text = failure.message ?: getString(R.string.invalid_address) }
            }
        })
    }

    private fun privateHost(host: String): Boolean {
        if (host == "localhost") return true
        val parts = host.split('.').map { it.toIntOrNull() ?: return false }
        if (parts.size != 4 || parts.any { it !in 0..255 } || parts.joinToString(".") != host) return false
        return parts[0] == 10 || parts[0] == 127 ||
            (parts[0] == 192 && parts[1] == 168) || (parts[0] == 172 && parts[1] in 16..31) ||
            (parts[0] == 100 && parts[1] in 64..127)
    }

    private fun sameOrigin(value: String): Boolean = try {
        val a = URI(origin); val b = URI(value)
        fun port(uri: URI) = if (uri.port >= 0) uri.port else if (uri.scheme == "https") 443 else 80
        a.scheme == b.scheme && a.host.equals(b.host, ignoreCase = true) && port(a) == port(b) && b.rawUserInfo == null
    } catch (_: Exception) { false }

    @SuppressLint("SetJavaScriptEnabled")
    private fun openMonitor() {
        val root = layout()
        val toolbar = LinearLayout(this)
        val status = TextView(this).apply { text = origin; textSize = 11f; setPadding(dp(12), dp(8), dp(12), dp(8)) }
        toolbar.addView(Button(this).apply { setText(R.string.switch_controller); setOnClickListener { showConnection() } })
        toolbar.addView(Button(this).apply { setText(R.string.reload); setOnClickListener { status.text = origin; browser?.reload() } })
        root.addView(toolbar); root.addView(status)
        val web = WebView(this)
        browser = web
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            javaScriptCanOpenWindowsAutomatically = false
            setSupportMultipleWindows(false)
        }
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false)
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean = !sameOrigin(request.url.toString())
            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
                if (sameOrigin(request.url.toString())) return null
                return WebResourceResponse("text/plain", "UTF-8", 403, "Forbidden", emptyMap(), "External resource blocked".byteInputStream())
            }
            override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
                handler.cancel()
                status.setText(R.string.certificate_error)
            }
            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) status.setText(R.string.connection_error)
            }
            override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, response: WebResourceResponse) {
                if (request.isForMainFrame) status.text = getString(R.string.http_error, response.statusCode)
            }
        }
        root.addView(web, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        web.loadUrl("$origin/mobile/")
    }

    @Deprecated("Platform fallback is used on Android 9 and later")
    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        val web = browser ?: return super.onBackPressed()
        web.evaluateJavascript("Boolean(window.ExperimentMobileBack && window.ExperimentMobileBack())") { handled ->
            if (browser === web && handled != "true") showConnection()
        }
    }
    override fun onPause() { browser?.onPause(); super.onPause() }
    override fun onResume() { super.onResume(); browser?.onResume() }
    override fun onDestroy() { browser?.destroy(); browser = null; super.onDestroy() }
}
