using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;

namespace ExperimentManagerDesktop {
    internal sealed class UpdateRelease {
        internal string Version, Tag, ReleaseUrl, Notes, AssetName, AssetUrl, ChecksumUrl, Sha256;
        internal long Size;
    }

    // GitHub public Releases API: https://docs.github.com/en/rest/releases/releases#list-releases
    // No account token or application credentials are sent by this client.
    internal static class UpdateService {
        internal const string Repository = "https://github.com/Pencilfinely/experiment-manager";
        internal const string ReleasesApi = "https://api.github.com/repos/Pencilfinely/experiment-manager/releases?per_page=100";
        internal const long MaxInstallerBytes = 1024L * 1024L * 1024L;
        const int MaxApiBytes = 8 * 1024 * 1024;
        const int MaxChecksumBytes = 256 * 1024;
        static readonly Regex VersionPattern = new Regex(@"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$", RegexOptions.CultureInvariant);
        static readonly Regex HashPattern = new Regex("^[0-9a-fA-F]{64}$", RegexOptions.CultureInvariant);

        internal static UpdateRelease Check(string currentVersion, bool worker) {
            try {
                var release = SelectRelease(ReadText(ReleasesApi, true, MaxApiBytes), currentVersion, worker);
                if(release != null) release.Sha256 = ParseChecksum(ReadText(release.ChecksumUrl, false, MaxChecksumBytes), release.AssetName);
                return release;
            } catch(WebException ex) { throw NetworkError(ex); }
        }

        internal static string Download(UpdateRelease release, string cacheRoot, Action<long,long> progress, CancellationToken cancellation) {
            ValidateRelease(release, true);
            cancellation.ThrowIfCancellationRequested();
            Directory.CreateDirectory(cacheRoot);
            string target = Path.Combine(Path.GetFullPath(cacheRoot), release.AssetName);
            if(File.Exists(target)) {
                try {
                    ValidateFile(release, target, cancellation);
                    if(progress != null) progress(release.Size, release.Size);
                    return target;
                } catch(InvalidDataException) { /* Replace an incomplete or obsolete cached download. */ }
            }
            try {
                // Abort also interrupts an active synchronous Read when the user cancels.
                using(var response = OpenResponse(release.AssetUrl, false, cancellation)) {
                    if(response.ContentLength >= 0 && response.ContentLength != release.Size)
                        throw new InvalidDataException("下载大小与发布信息不一致，请稍后重新检查更新。");
                    using(var stream = response.GetResponseStream())
                        return SaveDownload(stream, release, target, progress, cancellation);
                }
            } catch(WebException ex) {
                cancellation.ThrowIfCancellationRequested();
                throw NetworkError(ex);
            } catch(IOException) {
                cancellation.ThrowIfCancellationRequested();
                throw;
            }
        }

        internal static void ValidateDownloaded(UpdateRelease release, string path) {
            ValidateRelease(release, true);
            ValidateFile(release, path, CancellationToken.None);
        }

        internal static UpdateRelease SelectRelease(string json, string currentVersion, bool worker) {
            var current = ParseVersion(currentVersion);
            object parsed;
            try { parsed = new JavaScriptSerializer { MaxJsonLength = MaxApiBytes }.DeserializeObject(json); }
            catch(Exception ex) { throw new InvalidDataException("GitHub 更新信息无法解析，请稍后重试。", ex); }
            var releases = parsed as object[];
            if(releases == null) throw new InvalidDataException("GitHub 返回的更新列表格式不正确。");
            UpdateRelease selected = null;
            foreach(object item in releases) {
                var data = item as Dictionary<string,object>;
                if(data == null || Flag(data, "draft")) continue;
                string tag = Text(data, "tag_name");
                SemanticVersion version;
                try { version = ParseVersion(tag); } catch(FormatException) { continue; }
                if(current.Prerelease.Length == 0 && (version.Prerelease.Length != 0 || Flag(data, "prerelease"))) continue;
                if(Compare(version, current) <= 0 || (selected != null && CompareVersions(version.Value, selected.Version) <= 0)) continue;
                string name = "ExperimentManager-" + version.Value + "-windows-" + (worker ? "worker" : "controller") + "-x64-Setup.exe";
                var assets = data.ContainsKey("assets") ? data["assets"] as object[] : null;
                if(assets == null) continue;
                Dictionary<string,object> installer = null, checksums = null;
                foreach(object asset in assets) {
                    var fields = asset as Dictionary<string,object>;
                    if(fields == null || Text(fields, "state") != "uploaded") continue;
                    if(Text(fields, "name") == name) {
                        if(installer != null) throw new InvalidDataException("发布包含重复安装包，请联系维护者。");
                        installer = fields;
                    }
                    if(Text(fields, "name") == "SHA256SUMS.txt") {
                        if(checksums != null) throw new InvalidDataException("发布包含重复校验文件，请联系维护者。");
                        checksums = fields;
                    }
                }
                if(installer == null || checksums == null) continue;
                long size;
                if(!Int64.TryParse(Text(installer, "size"), NumberStyles.None, CultureInfo.InvariantCulture, out size))
                    throw new InvalidDataException("安装包大小无效，请联系维护者。");
                var release = new UpdateRelease { Version = version.Value, Tag = tag,
                    ReleaseUrl = Repository + "/releases/tag/" + Uri.EscapeDataString(tag),
                    Notes = Text(data, "body"), AssetName = name, AssetUrl = Text(installer, "browser_download_url"),
                    ChecksumUrl = Text(checksums, "browser_download_url"), Size = size };
                ValidateRelease(release, false);
                selected = release;
            }
            return selected;
        }

        internal static string ParseChecksum(string text, string assetName) {
            string found = null;
            using(var reader = new StringReader(text.TrimStart('\uFEFF'))) {
                string line;
                while((line = reader.ReadLine()) != null) {
                    // Accept sha256sum's text (two spaces) and binary (space, star) forms.
                    var match = Regex.Match(line, @"^([0-9a-fA-F]{64}) [ *](.+)$", RegexOptions.CultureInvariant);
                    if(!match.Success || match.Groups[2].Value != assetName) continue;
                    if(found != null) throw new InvalidDataException("安装包的 SHA256 校验条目重复，请联系维护者。");
                    found = match.Groups[1].Value.ToLowerInvariant();
                }
            }
            if(found == null) throw new InvalidDataException("发布中缺少此安装包的 SHA256 校验值，无法安全下载更新。");
            return found;
        }

        internal static int CompareVersions(string left, string right) { return Compare(ParseVersion(left), ParseVersion(right)); }

        // Kept separate from HTTP so truncation, cancellation and atomic finalization can
        // be checked against deterministic streams without network or installation.
        internal static string SaveDownload(Stream input, UpdateRelease release, string target, Action<long,long> progress, CancellationToken cancellation) {
            string partial = target + "." + Guid.NewGuid().ToString("N") + ".part";
            var timer = Stopwatch.StartNew();
            try {
                cancellation.ThrowIfCancellationRequested();
                long total = 0;
                byte[] buffer = new byte[128 * 1024];
                using(var output = new FileStream(partial, FileMode.CreateNew, FileAccess.Write, FileShare.None)) {
                    while(true) {
                        cancellation.ThrowIfCancellationRequested();
                        if(timer.Elapsed > TimeSpan.FromMinutes(20)) throw new IOException("下载更新超时，请检查网络后重试。");
                        int count = input.Read(buffer, 0, buffer.Length);
                        if(count == 0) break;
                        total += count;
                        if(total > release.Size || total > MaxInstallerBytes) throw new InvalidDataException("安装包超过声明大小，下载已停止。");
                        output.Write(buffer, 0, count);
                        if(progress != null) progress(total, release.Size);
                    }
                    output.Flush(true);
                }
                ValidateFile(release, partial, cancellation);
                cancellation.ThrowIfCancellationRequested();
                if(File.Exists(target)) {
                    // A failed cache is expendable; the verified temporary file is
                    // published with one same-volume rename. File.Replace also tries
                    // to merge Windows ACL metadata, which is unavailable in some
                    // restricted user profiles.
                    File.Delete(target);
                }
                File.Move(partial, target);
                return target;
            } finally {
                if(File.Exists(partial)) File.Delete(partial);
            }
        }

        internal static void ValidateFile(UpdateRelease release, string path, CancellationToken cancellation) {
            if(release.Size < 2 || release.Size > MaxInstallerBytes || !HashPattern.IsMatch(release.Sha256 ?? ""))
                throw new InvalidDataException("安装包校验信息无效，请重新检查更新。");
            cancellation.ThrowIfCancellationRequested();
            using(var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read)) {
                if(stream.Length != release.Size) throw new InvalidDataException("安装包下载不完整，请重新下载。");
                if(stream.ReadByte() != 'M' || stream.ReadByte() != 'Z') throw new InvalidDataException("下载文件不是有效的 Windows 安装程序。");
                stream.Position = 0;
                using(var sha = SHA256.Create()) {
                    byte[] buffer = new byte[128 * 1024];
                    int count;
                    while((count = stream.Read(buffer, 0, buffer.Length)) != 0) {
                        cancellation.ThrowIfCancellationRequested();
                        sha.TransformBlock(buffer, 0, count, buffer, 0);
                    }
                    sha.TransformFinalBlock(new byte[0], 0, 0);
                    string actual = BitConverter.ToString(sha.Hash).Replace("-", "");
                    if(!String.Equals(actual, release.Sha256, StringComparison.OrdinalIgnoreCase))
                        throw new InvalidDataException("安装包 SHA256 校验失败，请重新下载；若再次失败，请联系维护者。");
                }
            }
        }

        static void ValidateRelease(UpdateRelease release, bool requireHash) {
            if(release == null) throw new InvalidDataException("没有可下载的更新，请先检查更新。");
            var version = ParseVersion(release.Tag);
            if(version.Value != release.Version) throw new InvalidDataException("更新版本与发布标签不一致。");
            string prefix = "ExperimentManager-" + version.Value + "-windows-";
            if(release.AssetName != prefix + "controller-x64-Setup.exe" && release.AssetName != prefix + "worker-x64-Setup.exe")
                throw new InvalidDataException("更新安装包名称不正确。");
            ValidateAssetUri(release.AssetUrl, release.Tag, release.AssetName);
            ValidateAssetUri(release.ChecksumUrl, release.Tag, "SHA256SUMS.txt");
            if(release.Size < 2 || release.Size > MaxInstallerBytes) throw new InvalidDataException("更新安装包大小超出允许范围。");
            if(requireHash && !HashPattern.IsMatch(release.Sha256 ?? "")) throw new InvalidDataException("更新缺少有效的 SHA256 校验值。");
        }

        internal static void ValidateAssetUri(string url, string tag, string name) {
            string expected = Repository + "/releases/download/" + Uri.EscapeDataString(tag) + "/" + Uri.EscapeDataString(name);
            Uri actual;
            if(!Uri.TryCreate(url, UriKind.Absolute, out actual) || actual.Scheme != "https" || !actual.IsDefaultPort ||
                actual.UserInfo.Length != 0 || !String.Equals(actual.AbsoluteUri, expected, StringComparison.Ordinal))
                throw new InvalidDataException("更新下载地址不属于项目的 GitHub 发布，已停止操作。");
        }

        static string ReadText(string url, bool api, int limit) {
            using(var response = OpenResponse(url, api, CancellationToken.None)) {
                if(response.ContentLength > limit) throw new InvalidDataException("更新信息过大，已停止读取。");
                using(var stream = response.GetResponseStream()) using(var memory = new MemoryStream()) {
                    byte[] buffer = new byte[16 * 1024];
                    var timer = Stopwatch.StartNew();
                    int count;
                    while((count = stream.Read(buffer, 0, buffer.Length)) != 0) {
                        if(timer.Elapsed > TimeSpan.FromSeconds(60)) throw new IOException("读取更新信息超时，请稍后重试。");
                        if(memory.Length + count > limit) throw new InvalidDataException("更新信息过大，已停止读取。");
                        memory.Write(buffer, 0, count);
                    }
                    return new UTF8Encoding(false, true).GetString(memory.ToArray());
                }
            }
        }

        sealed class DownloadResponse : IDisposable {
            internal HttpWebResponse Response;
            internal CancellationTokenRegistration Registration;
            internal long ContentLength { get { return Response.ContentLength; } }
            internal Stream GetResponseStream() { return Response.GetResponseStream(); }
            public void Dispose() { try { Response.Dispose(); } finally { Registration.Dispose(); } }
        }

        static DownloadResponse OpenResponse(string url, bool api, CancellationToken cancellation) {
            ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
            var uri = new Uri(url);
            for(int hop = 0; hop <= 5; hop++) {
                cancellation.ThrowIfCancellationRequested();
                if(!AllowedRequestUri(uri, api, hop == 0)) throw new InvalidDataException("更新下载重定向到不受信任的地址，已停止操作。");
                var request = (HttpWebRequest)WebRequest.Create(uri);
                request.Method = "GET";
                request.UserAgent = "ExperimentManager-Desktop-Updater";
                request.Accept = api ? "application/vnd.github+json" : "application/octet-stream";
                request.Headers["X-GitHub-Api-Version"] = "2022-11-28";
                request.AllowAutoRedirect = false;
                request.Timeout = 30000;
                request.ReadWriteTimeout = 30000;
                request.AutomaticDecompression = DecompressionMethods.GZip | DecompressionMethods.Deflate;
                var registration = cancellation.Register(request.Abort);
                HttpWebResponse response = null;
                try {
                    response = (HttpWebResponse)request.GetResponse();
                    int status = (int)response.StatusCode;
                    if(status == 301 || status == 302 || status == 303 || status == 307 || status == 308) {
                        string location = response.Headers[HttpResponseHeader.Location];
                        Uri next;
                        if(String.IsNullOrEmpty(location) || !Uri.TryCreate(uri, location, out next)) throw new InvalidDataException("更新服务器返回了无效的重定向地址。");
                        uri = next;
                        response.Dispose(); response = null;
                        registration.Dispose();
                        continue;
                    }
                    if(status != 200) throw new InvalidDataException("更新服务器返回了意外的响应，请稍后重试。");
                    return new DownloadResponse { Response = response, Registration = registration };
                } catch {
                    if(response != null) response.Dispose();
                    registration.Dispose();
                    throw;
                }
            }
            throw new InvalidDataException("更新下载重定向次数过多，请稍后重试。");
        }

        internal static bool AllowedRequestUri(Uri uri, bool api, bool initial) {
            if(uri.Scheme != "https" || !uri.IsDefaultPort || uri.UserInfo.Length != 0 || uri.Fragment.Length != 0) return false;
            if(api) return uri.AbsoluteUri == ReleasesApi;
            if(uri.Host == "github.com") return uri.AbsolutePath.StartsWith("/Pencilfinely/experiment-manager/releases/download/", StringComparison.Ordinal) && uri.Query.Length == 0;
            return !initial && (uri.Host == "release-assets.githubusercontent.com" || uri.Host == "objects.githubusercontent.com");
        }

        static Exception NetworkError(WebException ex) {
            var response = ex.Response as HttpWebResponse;
            string message = "无法连接 GitHub 更新服务器，请检查网络后重试。";
            if(ex.Status == WebExceptionStatus.Timeout) message = "连接 GitHub 更新服务器超时，请稍后重试。";
            else if(response != null && ((int)response.StatusCode == 403 || (int)response.StatusCode == 429)) message = "GitHub 暂时限制了更新请求，请稍后重试。";
            else if(response != null && response.StatusCode == HttpStatusCode.NotFound) message = "GitHub 发布或安装包不存在，请重新检查更新。";
            if(response != null) response.Dispose();
            return new IOException(message, ex);
        }

        sealed class SemanticVersion {
            internal string Value;
            internal string[] Numbers, Prerelease;
        }
        static SemanticVersion ParseVersion(string value) {
            if(value == null) throw new FormatException("当前版本号无效，无法检查更新。");
            if(value.StartsWith("v", StringComparison.Ordinal)) value = value.Substring(1);
            var match = VersionPattern.Match(value);
            if(!match.Success) throw new FormatException("版本号格式无效：" + value);
            string[] pre = match.Groups[4].Success ? match.Groups[4].Value.Split('.') : new string[0];
            foreach(string identifier in pre)
                if(Numeric(identifier) && identifier.Length > 1 && identifier[0] == '0') throw new FormatException("预览版本号不能包含前导零。");
            return new SemanticVersion { Value = value, Numbers = new[] { match.Groups[1].Value, match.Groups[2].Value, match.Groups[3].Value }, Prerelease = pre };
        }
        static bool Numeric(string value) {
            foreach(char c in value) if(c < '0' || c > '9') return false;
            return value.Length != 0;
        }
        static int NumericCompare(string left, string right) {
            int comparison = left.Length.CompareTo(right.Length);
            return comparison != 0 ? comparison : String.CompareOrdinal(left, right);
        }
        static int Compare(SemanticVersion left, SemanticVersion right) {
            for(int i = 0; i < 3; i++) { int comparison = NumericCompare(left.Numbers[i], right.Numbers[i]); if(comparison != 0) return comparison; }
            if(left.Prerelease.Length == 0 || right.Prerelease.Length == 0)
                return left.Prerelease.Length == right.Prerelease.Length ? 0 : (left.Prerelease.Length == 0 ? 1 : -1);
            for(int i = 0; i < Math.Min(left.Prerelease.Length, right.Prerelease.Length); i++) {
                string a = left.Prerelease[i], b = right.Prerelease[i];
                bool an = Numeric(a), bn = Numeric(b);
                int comparison = an && bn ? NumericCompare(a, b) : (an != bn ? (an ? -1 : 1) : String.CompareOrdinal(a, b));
                if(comparison != 0) return comparison;
            }
            return left.Prerelease.Length.CompareTo(right.Prerelease.Length);
        }
        static string Text(Dictionary<string,object> data, string key) {
            object value;
            return data.TryGetValue(key, out value) && value != null ? Convert.ToString(value, CultureInfo.InvariantCulture) : "";
        }
        static bool Flag(Dictionary<string,object> data, string key) {
            object value;
            return data.TryGetValue(key, out value) && value is bool && (bool)value;
        }
    }
}
