# set arg parser -> store hyperparameter
# set PSNR, SSIM evaluation -> automation

import argparse
import os
from pathlib import Path
import time

import numpy as np
import cv2
import skimage.io as io
from skimage.metrics import peak_signal_noise_ratio as psnr
from skimage.metrics import structural_similarity as ssim
from sklearn.decomposition import PCA
from sklearn.feature_extraction import image

from metrics import calculate_psnr, calculate_ssim, skim_compare_psnr, skim_compare_ssim
from utils import add_noise, load_gray_img

def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument("--input_dir", type=Path, default="./input/clean")
    parser.add_argument("--sigmas", type=int, nargs="+", help = "noise level of images")
    parser.add_argument("--output_dir", type=Path, default="./output")

    # hyperparameter
    parser.add_argument("--K", type=int, default=3)
    parser.add_argument("--L", type=int, default=9)
    parser.add_argument("--c", type= int, default=8)
    parser.add_argument("--c_s", help="estimation error of noiseless images", default=0.35, type=float)

    

    args = parser.parse_args()
    return args


def vector_pixel(x,y,k,image):
    # generate a vector variable of a pixel with coordinate (x,y) 
    # and window length k
    halfk = k // 2
    l = []
    for i in range(y-halfk, y+halfk+1):
        for j in range(x-halfk, x+halfk+1):
            l.append(image[j][i])
    return np.array(l)

def LPG_error(array1, array2):
    # calculate the difference 
    # between vector variable 
    # and training sample
    err = 0
    for i in range(len(array1)):
        err += (array1[i] - array2[i])**2
    return err/len(array1)

def select_training_samples(x,y,k,l,t,image,cm):
    # select training samples for the central pixel (x,y)
    # with the window size k and l, threshold t
    l_training_samples = []
    l_error = []
    halfk = k // 2
    halfl = l // 2

    # TODO: don't need to compute the center block every time
    for i in range(y-halfl+halfk, y+halfl-halfk+1):    #y
        for j in range(x-halfl+halfk, x+halfl-halfk+1):    #x
            error = LPG_error(
                vector_pixel(j,i,k,image), 
                vector_pixel(x,y,k,image)
                )
            if j!= x or i != y:
                l_training_samples.append(vector_pixel(j,i,k,image))
                l_error.append(error)
    
    pairs = zip(l_training_samples, l_error)
    
    # Sort the pairs based on the values in l_error
    sorted_pairs = sorted(pairs, key=lambda x: x[1])
    
    # Unpack the sorted pairs to get the sorted l_training_samples
    sorted_samples = [pair[0] for pair in sorted_pairs]
    sorted_error = [pair[1] for pair in sorted_pairs]
    
    #find the largest error in error list that are smaller than T
    index_error = 0
    while sorted_error[index_error] < t and index_error < len(sorted_error)-1:
        index_error += 1
            
    # make sure we have at least cm training examples
    if index_error < cm:
        sorted_samples = sorted_samples[:cm]
    else:
        sorted_samples = sorted_samples[:index_error]
    
    return np.array(sorted_samples)

def get_block_for_one_pixel(img, x, y, half_k):
    block = img[x-half_k: x+half_k+1, y-half_k: y+half_k+1]
    return block

def get_all_training_features(img, x, y, K, L):
    print(img.shape)
    dim1, dim2 = img.shape
    half_l = L // 2

    # print("position: ", x, y)

    # deal with edges
    x_min = 0 if x-half_l < 0 else x-half_l
    x_max = dim1 if x+half_l > dim1 else x+half_l
    y_min = 0 if y-half_l < 0 else y-half_l
    y_max = dim2 if y+half_l > dim2 else y+half_l


    # halfK = K // 2
    # rng = half_l - halfK
    
    # x_min = max(K, x - rng) - halfK
    # x_max = min(x + rng + 1, dim2 - K) + halfK
    # y_min = max(K, y - rng) - halfK
    # y_max = min(y + rng + 1, dim1 - K) + halfK
    
    # print(x_min, x_max, y_min, y_max)

    training_block = img[
        x_min:x_max, y_min:y_max
        ]
    training_features= image.extract_patches_2d(
        training_block, (K, K)
    ).reshape(-1, K, K)

    return training_features

def get_PCA_training_features(c, K, training_features, target):
    # Sort by MSE

    cm = c * (K ** 2)
    n = cm if cm < training_features.shape[0] else training_features.shape[0]

    square_err = ((training_features - target)**2)
    mse = np.mean(
        square_err.reshape(-1, K**2), axis=1
    )

    sort_indexes = np.argsort(mse)

    # (n, K^2)
    training_features_PCA = training_features[sort_indexes[:n], :, :] \
        .reshape(n, target.shape[0]**2)
    return training_features_PCA

def PCA_denoise(X, sigma):
    
    X = X.swapaxes(1, 0) # (K^2, n)
    X_mean = np.mean(X, axis=1).reshape(-1, 1)   # (K^2, )
    X = X - X_mean

    cov_sigma = sigma**2 * np.eye(X.shape[0], X.shape[0]) # sigma^2 * I, (K^2, K^2)
    sigma_X = np.cov(X) # sigma_x^bar, (K^2, K^2)
    eigen_X = np.linalg.eig(sigma_X)[1] # phi_x_bar, (K^2, K^2)
    PX = eigen_X.T # 3.9 (K^2, K^2)

    Y_v_bar = PX @ X # 3.9 - 2, (K^2, n)
    sigma_v = PX @ cov_sigma @ PX.T # 3.7 - 2, (K^2, K^2)

    # correspond to "In implementation, we first calculate ..."
    sigma_y_v_bar = (Y_v_bar @ Y_v_bar.T)/X.shape[0] # 3.10
    phi_y_bar = np.maximum( 
        np.zeros(sigma_y_v_bar.shape), 
        sigma_y_v_bar - sigma_v 
        ) # 3.12
    
    # dim = (K^2, )
    shrinkage_coef = np.diag(
        phi_y_bar
        # the cov matrix of centralized data matrix is the same as that of original data matrix
        )/(np.diag(phi_y_bar) + np.diag(sigma_y_v_bar)) # 3.12
    
    # dim = (K^2, )
    denoise_X = PX.T @ (Y_v_bar * shrinkage_coef.reshape(-1, 1)) # 3.13
    denoise_X += X_mean
    denoise_pixel = denoise_X[denoise_X.shape[0]//2, 0] # retrieves the element in the middle of the X1 array
    return denoise_pixel

def denoise_one_pixel(img, x, y, K, L, c, sigma):
    # x, y = position of denoised pixel
    half_k = K // 2
    half_l = L // 2

    # Block centered around x,y, dim = (K, K)
    target_block = get_block_for_one_pixel(img, x, y, half_k)
    
    # All Training features, dim = (-1, K, K)
    all_training_features = get_all_training_features(img, x, y, K, L)

    # sort and select top n, dim = (n, K^2)
    PCA_features = get_PCA_training_features(c, K, all_training_features, target_block)

    # denoise, dim = (K^2, )
    denoise_pixel = PCA_denoise(PCA_features, sigma)
    return denoise_pixel

def denoise_image(img, K, L, c, sigma): 
    half_k = K//2
    out_img = np.copy(img)
    for x in range(half_k, img.shape[0] - half_k):
        for y in range(half_k, img.shape[1] - half_k):
            out_img[x, y] = denoise_one_pixel(img, x, y, K, L, c, sigma)
    
    return out_img

def main():
    args = parse_args()

    in_images_rel = [f for f in os.listdir(args.input_dir)]

    for sigma in args.sigmas:

        out_dir = os.path.join(args.output_dir, f"gauss_{args.sigma}")
        os.makedirs(out_dir, exist_ok=True)
        x = time.time() 
        for img_path in in_images_rel:

            print(f"Denoising {img_path}")
            in_path = os.path.join(args.input_dir, img_path)
            clean_img = io.imread(in_path)
            noisy_img = add_noise(clean_img, sigma)

            # TODO: storenoisy image

            # psnr = skim_compare_psnr(clean_img[:, :, 0], noisy_img[:, :, 0])
            # ssim = skim_compare_ssim(clean_img[:, :, 0], noisy_img[:, :, 0])
            # print(f"PSNR: {psnr}, SSIM: {ssim}")
            # exit()      


            stage_1_denoised_img = denoise_image(
                noisy_img, args.K, args.L, 
                args.c, args.sigma
                )
            
            ## TODO: problem?
            sigma_2 = args.c_s * np.sqrt(
                args.sigma**2 - np.mean(
                    (noisy_img - stage_1_denoised_img)**2
                    )
                )
            
            # print(sigma_2)
            
            stage_2_denoised_img = denoise_image(
                stage_1_denoised_img, args.K, args.L, 
                args.c, sigma_2
                )
            
            out_path_1 = os.path.join(out_dir, f"stage_1_{img_path}")
            out_path_2 = os.path.join(out_dir, f"stage_2_{img_path}")

            cv2.imwrite(out_path_1, stage_1_denoised_img)      
            cv2.imwrite(out_path_2, stage_2_denoised_img)
            
            y = time.time()
            print(f"{round((y-x)/60, 4)} mins used")

            # print(stage_1_denoised_img)

            # calculate PSNR, SSIM
            psnr_stage_1 = skim_compare_psnr(clean_img, stage_1_denoised_img)
            psnr_stage_2 = skim_compare_psnr(clean_img, stage_2_denoised_img)
            ssim_stage_1 = skim_compare_ssim(clean_img, stage_1_denoised_img)
            ssim_stage_2 = skim_compare_ssim(clean_img, stage_2_denoised_img)

            print(f"First stage denoise result - PNSR: {psnr_stage_1}, SSIM: {ssim_stage_1}")
            print(f"Second stage denoise result - PNSR: {psnr_stage_2}, SSIM: {ssim_stage_2}")

if __name__ == "__main__":
    main() 