import argparse
import os
import sys
import shutil
import models
import numpy as np
import tensorflow as tf
import scipy.misc
from data import DataSet
os.environ["TF_CPP_MIN_LOG_LEVEL"]='2'
import torch
import torch.nn.functional as F

def _fspecial_gauss_1d(size, sigma):
    coords = torch.arange(size).to(dtype=torch.float)
    coords -= size//2
    g = torch.exp(-(coords**2) / (2*sigma**2))
    g /= g.sum()
    return g.unsqueeze(0).unsqueeze(0)
    
def gaussian_filter(input, win):
    N, C, H, W = input.shape
    out = F.conv2d(input, win, stride=1, padding=0, groups=C)
    out = F.conv2d(out, win.transpose(2, 3), stride=1, padding=0, groups=C)
    return out


def _ssim(X, Y, win, data_range=1023, size_average=True, full=False):
    K1 = 0.01
    K2 = 0.03
    batch, channel, height, width = X.shape
    compensation = 1.0

    C1 = (K1 * data_range)**2
    C2 = (K2 * data_range)**2

    win = win.to(X.device, dtype=X.dtype)

    mu1 = gaussian_filter(X, win)
    mu2 = gaussian_filter(Y, win)

    mu1_sq = mu1.pow(2)
    mu2_sq = mu2.pow(2)
    mu1_mu2 = mu1 * mu2

    sigma1_sq = compensation * ( gaussian_filter(X * X, win) - mu1_sq )
    sigma2_sq = compensation * ( gaussian_filter(Y * Y, win) - mu2_sq )
    sigma12   = compensation * ( gaussian_filter(X * Y, win) - mu1_mu2 )

    cs_map = (2 * sigma12 + C2) / (sigma1_sq + sigma2_sq + C2)
    ssim_map = ((2 * mu1_mu2 + C1) / (mu1_sq + mu2_sq + C1)) * cs_map

    if size_average:
        ssim_val = ssim_map.mean()
        cs = cs_map.mean()
    else:
        ssim_val = ssim_map.mean(-1).mean(-1).mean(-1)  # reduce along CHW
        cs = cs_map.mean(-1).mean(-1).mean(-1)

    if full:
        return ssim_val, cs
    else:
        return ssim_val


def ssim(X, Y, win_size=11, win_sigma=10, win=None, data_range=1, size_average=True, full=False):

    if len(X.shape) != 4:
        raise ValueError('Input images must 4-d tensor.')

    if not X.type() == Y.type():
        raise ValueError('Input images must have the same dtype.')

    if not X.shape == Y.shape:
        raise ValueError('Input images must have the same dimensions.')

    if not (win_size % 2 == 1):
        raise ValueError('Window size must be odd.')

    win_sigma = win_sigma
    if win is None:
        win = _fspecial_gauss_1d(win_size, win_sigma)
        win = win.repeat(X.shape[1], 1, 1, 1)
    else:
        win_size = win.shape[-1]

    ssim_val, cs = _ssim(X, Y,
                         win=win,
                         data_range=data_range,
                         size_average=False,
                         full=True)
    if size_average:
        ssim_val = ssim_val.mean()
        cs = cs.mean()

    if full:
        return ssim_val, cs
    else:
        return ssim_val


def ms_ssim(X, Y, win_size=11, win_sigma=10, win=None, data_range=1, size_average=True, full=False, weights=None):
    if len(X.shape) != 4:
        raise ValueError('Input images must 4-d tensor.')

    if not X.type() == Y.type():
        raise ValueError('Input images must have the same dtype.')

    if not X.shape == Y.shape:
        raise ValueError('Input images must have the same dimensions.')

    if not (win_size % 2 == 1):
        raise ValueError('Window size must be odd.')

    if weights is None:
        weights = torch.FloatTensor(
            [0.0448, 0.2856, 0.3001, 0.2363, 0.1333]).to(X.device, dtype=X.dtype)

    win_sigma = win_sigma
    if win is None:
        win = _fspecial_gauss_1d(win_size, win_sigma)
        win = win.repeat(X.shape[1], 1, 1, 1)
    else:
        win_size = win.shape[-1]

    levels = weights.shape[0]
    mcs = []
    for _ in range(levels):
        ssim_val, cs = _ssim(X, Y,
                             win=win,
                             data_range=data_range,
                             size_average=False,
                             full=True)
        mcs.append(cs)

        padding = (X.shape[2] % 2, X.shape[3] % 2)
        X = F.avg_pool2d(X, kernel_size=2, padding=padding)
        Y = F.avg_pool2d(Y, kernel_size=2, padding=padding)

    mcs = torch.stack(mcs, dim=0)  # mcs, (level, batch)
    # weights, (level)
    msssim_val = torch.prod((mcs[:-1] ** weights[:-1].unsqueeze(1))
                            * (ssim_val ** weights[-1]), dim=0)  # (batch, )

    if size_average:
        msssim_val = msssim_val.mean()
    return msssim_val


# Classes to re-use window
class SSIM(torch.nn.Module):
    def __init__(self, win_size=11, win_sigma=1.5, data_range=255, size_average=True, channel=3):
        super(SSIM, self).__init__()
        self.win = _fspecial_gauss_1d(
            win_size, win_sigma).repeat(channel, 1, 1, 1)
        self.size_average = size_average
        self.data_range = data_range

    def forward(self, X, Y):
        return ssim(X, Y, win=self.win, data_range=self.data_range, size_average=self.size_average)


class MS_SSIM(torch.nn.Module):
    def __init__(self, win_size=11, win_sigma=1.5, data_range=255, size_average=True, channel=3, weights=None):
        super(MS_SSIM, self).__init__()
        self.win = _fspecial_gauss_1d(
            win_size, win_sigma).repeat(channel, 1, 1, 1)
        self.size_average = size_average
        self.data_range = data_range
        self.weights = weights
def calc_gradients(
        test_file,
        model_name,
        output_file_dir,
        max_iter,
        learning_rate=0.0001,
        targets=None,
        weight_loss2=1,
        data_spec=None,
        batch_size=1,
        seq_len=40):

    """Compute the gradients for the given network and images."""    
    spec = data_spec

    modifier = tf.Variable(0.01*np.ones((1, seq_len, spec.crop_size,spec.crop_size,spec.channels),dtype=np.float32))
    input_image = tf.placeholder(tf.float32, (batch_size, seq_len, spec.crop_size, spec.crop_size, spec.channels))
    input_label = tf.placeholder(tf.int32, (batch_size))

    # temporal mask, 1 indicates the selected frame
    indicator = [0,0,0,0,0,0,0,0,0,0,0,1,1,1,0,0,0,0,0,1,1,1,1,0,0,0,0,0,0,0,0,0,1,1,0,0,0,0,0,0]   

    true_image = tf.minimum(tf.maximum(modifier[0,0,:,:,:]+input_image[0,0,:,:,:]*255.0, -spec.mean+spec.rescale[0]), -spec.mean+spec.rescale[1])/255.0
    true_image = tf.expand_dims(true_image, 0)
    for ll in range(seq_len-1):
        if indicator[ll+1] == 1:
           mask_temp = tf.minimum(tf.maximum(modifier[0,ll+1,:,:,:]+input_image[0,ll+1,:,:,:]*255.0, -spec.mean+spec.rescale[0]), -spec.mean+spec.rescale[1])/255.0
        else:
           mask_temp = input_image[0,ll+1,:,:,:]
        mask_temp = tf.expand_dims(mask_temp,0)
        true_image = tf.concat([true_image, mask_temp],0)
    true_image = tf.expand_dims(true_image, 0)

    for kk in range(batch_size-1):
        true_image_temp = tf.minimum(tf.maximum(modifier[0,0,:,:,:]+input_image[kk+1,0,:,:,:]*255.0, -spec.mean+spec.rescale[0]), -spec.mean+spec.rescale[1])/255.0
        true_image_temp = tf.expand_dims(true_image_temp, 0)
        for ll in range(seq_len-1):
            if indicator[ll+1] == 1:
               mask_temp = tf.minimum(tf.maximum(modifier[0,ll+1,:,:,:]+input_image[kk+1,ll+1,:,:,:]*255.0, -spec.mean+spec.rescale[0]), -spec.mean+spec.rescale[1])/255.0
            else:
               mask_temp = input_image[kk+1,ll+1,:,:,:]
            mask_temp = tf.expand_dims(mask_temp,0)
            true_image_temp = tf.concat([true_image_temp, mask_temp],0)
        true_image_temp = tf.expand_dims(true_image_temp, 0)

        true_image = tf.concat([true_image, true_image_temp],0)
    loss2 = ssim(true_image, input_image, data_range=1, size_average=True,  )
    #loss2 = tf.reduce_sum(tf.sqrt(tf.reduce_mean(tf.square(true_image-input_image), axis=[0, 2, 3, 4])))
    norm_frame = tf.reduce_mean(tf.abs(modifier), axis=[2,3,4])

    sess = tf.Session()
    probs, variable_set, pre_label,ince_output, pre_node = models.get_model(sess, true_image, model_name, False) 
    true_label_prob = tf.reduce_sum(probs*tf.one_hot(input_label,101),[1])
    if targets is None:
        loss1 = -tf.log(1 - true_label_prob + 1e-6)
    else:
        loss1 = -tf.log(true_label_prob + 1e-6)
    loss1 = tf.reduce_mean(loss1)
    loss = loss1 + weight_loss2 * loss2

    optimizer = tf.train.AdamOptimizer(learning_rate)
    print('optimizer.minimize....')
    train = optimizer.minimize(loss, var_list=[modifier])
    # initiallize all uninitialized varibales
    init_varibale_list = set(tf.all_variables()) - variable_set
    sess.run(tf.initialize_variables(init_varibale_list))

    data = DataSet(test_list=test_file, seq_length=seq_len,image_shape=(spec.crop_size, spec.crop_size, spec.channels))
    all_names = []
    all_images = []
    all_labels = []
    
    def_len = 40
    for video in data.test_data:
        frames = data.get_frames_for_sample(video)
        if len(frames) < def_len:
           continue
        frames = data.rescale_list(frames, def_len)
        frames_data = data.build_image_sequence(frames)
        all_images.append(frames_data)
        label, hot_labels = data.get_class_one_hot(video[1])
        all_labels.append(label)
        all_names.append(frames)
    total = len(all_names)
    all_indices = range(total)
    num_batch = total/batch_size
    print('process data length:', num_batch)

    correct_ori = 0
    correct_noi = 0
    tot_image = 0
    
    for ii in range(num_batch):        
        images = all_images[ii*batch_size : (ii+1)*batch_size]
        names = all_names[ii*batch_size : (ii+1)*batch_size]
        labels = all_labels[ii*batch_size : (ii+1)*batch_size]
        indices = all_indices[ii*batch_size : (ii+1)*batch_size]
        print('------------------prediction for clean video-------------------')
        print('---video-level prediction---')
        for xx in range(len(indices)):
            print(names[xx][0],'label:', labels[xx], 'indice:',indices[xx], 'size:', len(images[xx]), len(images[xx][0]), len(images[xx][0][0]), len(images[xx][0][0][0]))
        sess.run(tf.initialize_variables(init_varibale_list))
        if targets is not None:
            labels = [targets[e] for e in names]
        
        feed_dict = {input_image: [images[0][0:seq_len]], input_label: labels}
        var_loss, true_prob, var_loss1, var_loss2, var_pre, var_node = sess.run((loss, true_label_prob, loss1, loss2, pre_label, pre_node), feed_dict=feed_dict)
        
        correct_pre = correct_ori
        for xx in range(len(indices)):
           if labels[xx] == var_pre[xx]:
              correct_ori += 1

        tot_image += 1
        print 'Start!'
        min_loss = var_loss
        last_min = -1
        print('---frame-wise prediction---')
        print('node_label:', var_node, 'label loss:', var_loss1, 'content loss:', var_loss2, 'prediction:', var_pre, 'probib', true_prob)
        # record numer of iteration
        tot_iter = 0

        if correct_pre == correct_ori:
           ii += 1
           continue
       
        print('------------------prediction for adversarial video-------------------')

        for cur_iter in range(max_iter):
            tot_iter += 1
            sess.run(train, feed_dict=feed_dict)
            var_loss, true_prob, var_loss1, var_loss2, var_pre, var_node = sess.run((loss, true_label_prob, loss1, loss2, pre_label, pre_node), feed_dict=feed_dict)
            print('iter:', cur_iter, 'total loss:', var_loss, 'label loss:', var_loss1, 'content loss:', var_loss2, 'prediction:', var_pre, 'probib:', true_prob)
            break_condition = False
            if var_loss < min_loss:
                if np.absolute(var_loss-min_loss) < 0.00001:
                   break_condition = True
                   print(last_min)
                min_loss = var_loss
                last_min = cur_iter

            if cur_iter + 1 == max_iter or break_condition:
                print('iter:', cur_iter, 'node_label:', var_node, 'label loss:', var_loss1, 'content loss:', var_loss2, 'prediction:', var_pre, 'probib:', true_prob)
                var_diff, var_probs, noise_norm = sess.run((modifier, probs, norm_frame), feed_dict=feed_dict)
                for pp in range(seq_len):
                    # print the map value for each frame
                    print(noise_norm[0][pp])
                for i in range(len(indices)):
                    top1 = var_probs[i].argmax()
                    if labels[i] == top1:
                        correct_noi += 1
                break
        print('saved modifier paramters.', ii)
        
        for ll in range(len(indices)):
            for kk in range(def_len):
                if kk < seq_len:
                   attack_img = np.clip(images[ll][kk]*255.0+var_diff[0][kk]+data_spec.mean,data_spec.rescale[0],data_spec.rescale[1])
                   diff = np.clip(np.absolute(var_diff[0][kk])*255.0, data_spec.rescale[0],data_spec.rescale[1])
                else:
                   attack_img = np.clip(images[ll][kk]*255.0+data_spec.mean,data_spec.rescale[0],data_spec.rescale[1])
                   diff = np.zeros((spec.crop_size,spec.crop_size,spec.channels))
                im_diff = scipy.misc.toimage(arr=diff, cmin=data_spec.rescale[0], cmax=data_spec.rescale[1])
                im = scipy.misc.toimage(arr=attack_img, cmin=data_spec.rescale[0], cmax=data_spec.rescale[1])
                new_name = names[ll][kk].split('/')
                 
                adv_dir = output_file_dir+'/adversarial/'
                dif_dir = output_file_dir+'/noise/'
                if not os.path.exists(adv_dir):
                   os.mkdir(adv_dir)
                   os.mkdir(dif_dir)

                tmp_dir = adv_dir+new_name[-2]
                tmp1_dir = dif_dir+new_name[-2]
                if not os.path.exists(tmp_dir):
                   os.mkdir(tmp_dir)
                   os.mkdir(tmp1_dir)
               
                new_name = new_name[-1] + '.png'
                im.save(tmp_dir + '/' +new_name)
                im_diff.save(tmp1_dir + '/' +new_name)
        print('saved adversarial frames.', ii)
        print('correct_ori:', correct_ori, 'correct_noi:', correct_noi)
           
def main():
    # Parse arguments
    parser = argparse.ArgumentParser(description='Use Adam optimizer to generate adversarial examples.')
    parser.add_argument('-i', '--input_dir', type=str, required=True,
                        help='Directory of dataset.')
    parser.add_argument('-o', '--output_dir', type=str, required=True,
                        help='Directory of output image file.')
    parser.add_argument('--model', type=str, required=True,choices=['GoogleNet','Inception2'],
                        help='Models to be evaluated.')
    parser.add_argument('--num_images', type=int, default=sys.maxsize,
                        help='Max number of images to be evaluated.')
    parser.add_argument('--file_list', type=str, default=None,
                        help='Evaluate a specific list of file in dataset.')
    parser.add_argument('--num_iter', type=int, default=100,
                        help='Number of iterations to generate attack.')
    parser.add_argument('--save_freq', type=int, default=100,
                        help='Save .npy file when each save_freq iterations.')
    parser.add_argument('--learning_rate', type=float, default=0.001 * 255,
                        help='Learning rate of each iteration.')
    parser.add_argument('--target', type=str, default=None,
                        help='Target list of dataset.')
    parser.add_argument('--weight_loss2', type=float, default=1.0,
                        help='Weight of distance penalty.')
    parser.add_argument('--not_crop', dest='use_crop', action='store_false',
                        help='Not use crop in image producer.')

    parser.set_defaults(use_crop=True)
    args = parser.parse_args()
    print(args.file_list)
    assert args.num_iter % args.save_freq == 0

    data_spec = models.get_data_spec(model_name=args.model)
    args.learning_rate = args.learning_rate / 255.0 * (data_spec.rescale[1] - data_spec.rescale[0])
    seq_len = 40
    batch_size = 1 
    targets = None
    if args.target is not None:
        targets = {}
        with open(args.target, 'r') as f:
            for line in f:
                key, value = line.strip().split()
                targets[key] = int(value)
                
    calc_gradients(
        args.file_list,
        args.model,
        args.output_dir,
        args.num_iter,
        args.learning_rate,
        targets,
        args.weight_loss2,
        data_spec,
        batch_size,
        seq_len)
    
if __name__ == '__main__':
    main()
