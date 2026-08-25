const params = Editor.getParams();
const refreshCounter =
    params.refreshCounter && params.refreshCounter[0]
        ? String(params.refreshCounter[0])
        : '0';

module.exports = {
    githubNews: {
        apiConnectionId: Editor.getId('githubNews'),
        path: '/news.json?refresh=' + encodeURIComponent(refreshCounter),
        method: 'GET',
        ui: true
    }
};
