# OTA QUICK SNIPER WEB

全国24場対応のスマホ向けWeb版です。

## Renderで公開する場合
1. このフォルダ一式をGitHubへアップロード
2. Renderで New > Web Service
3. GitHubリポジトリを選択
4. Build Command: `pip install -r requirements.txt`
5. Start Command: `gunicorn app:app`
6. Deploy

公開URLを相手へ送れば、Pythonista不要でSafariから利用できます。

## スマホ側
SafariでURLを開く → 共有 → 「ホーム画面に追加」
でアプリ風に使えます。

## 重要
同じレースへのアクセスは90秒間サーバーキャッシュします。
BOAT RACE公式への不要な連続アクセスを減らすためです。
