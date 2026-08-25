const params = Editor.getParams();
const currentValue =
    params.refreshCounter && params.refreshCounter[0]
        ? Number(params.refreshCounter[0])
        : 0;

const nextValue = String(
    Number.isFinite(currentValue)
        ? (currentValue + 1) % 1000000
        : 1
);

module.exports = {
    controls: [
        {
            type: 'button',
            param: 'refreshCounter',
            label: 'Обновить новости',
            theme: 'action',
            updateOnChange: true,
            onClick: {
                action: 'setParams',
                mode: 'merge',
                args: {
                    refreshCounter: [nextValue]
                }
            }
        }
    ]
};
