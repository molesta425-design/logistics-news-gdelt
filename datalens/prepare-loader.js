// В существующем Prepare замените код от первой строки до строки
// Editor.setRawData(news); включительно этим блоком. Весь код ниже оставьте.

const loaded = Editor.getLoadedData();
let result =
    loaded &&
    loaded.githubNews &&
    loaded.githubNews.data
        ? loaded.githubNews.data.body
        : null;

if (typeof result === 'string') {
    result = JSON.parse(result);
}

if (!result || !Array.isArray(result.news)) {
    throw new Error('GitHub не вернул корректный файл news.json');
}

const news = result.news;
const updatedAt = result.updatedAt || '';

Editor.setRawData(news);
