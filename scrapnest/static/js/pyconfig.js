// ブラウザ内の Python（Pyodide）の設定。版を上げるときはここだけ変える（sw.js と build_site.py もここを読む）
export const PYODIDE_VERSION = "314.0.8";
export const PYODIDE_URL = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;

// 写真の解析・輪郭・DXF の読み書きをする作業スレッドで読むもの（ezdxf は別に配っている wheel から入れる）
export const CORE_PACKAGES = [
  ["numpy", "pillow", "pyparsing", "typing-extensions", "fonttools", "micropip"],
  ["scipy", "shapely"],
  ["opencv-python"],
];
// 敷き詰めの作業スレッドで読むもの（DXF は扱わないので ezdxf は要らない）
export const NEST_PACKAGES = ["numpy", "scipy", "shapely", "opencv-python"];
