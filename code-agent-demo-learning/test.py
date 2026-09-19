def bubble_sort(arr):
    """
    冒泡排序
    :param arr: 待排序的列表
    :return: 排序后的列表（原地排序）
    """
    n = len(arr)
    for i in range(n - 1):
        # 标记本轮是否发生交换，用于优化
        swapped = False
        for j in range(n - 1 - i):
            if arr[j] > arr[j + 1]:
                arr[j], arr[j + 1] = arr[j + 1], arr[j]
                swapped = True
        # 如果本轮没有发生交换，说明已经有序，提前结束
        if not swapped:
            break
    return arr


if __name__ == "__main__":
    # 测试数据
    data = [64, 34, 25, 12, 22, 11, 90]
    print("排序前:", data)

    result = bubble_sort(data)
    print("排序后:", result)
