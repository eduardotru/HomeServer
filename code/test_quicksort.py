from quicksort import quicksort

if __name__ == "__main__":
    test_array = [3, 6, 8, 10, 1, 2, 1]
    sorted_array = quicksort(test_array)
    print(f"Original: {test_array}")
    print(f"Sorted: {sorted_array}")