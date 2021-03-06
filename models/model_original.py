import torch.nn as nn
import torch.nn.functional as F
import torch
from torch.autograd import Variable
import torchvision.transforms as transforms
from collections import OrderedDict
import numpy as np
import itertools
import visdom
from PIL import Image
import os
import time

def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1 or classname.find('Linear') != -1:
        m.weight.data.normal_(0.0, 0.02)
        m.bias.data.fill_(0)
    elif classname.find('BatchNorm') != -1:
        m.weight.data.normal_(1.0, 0.02)


class KeyPatchGanModel():
    def __init__(self):
        self.opts = []

    def initialize(self, opts):
        self.opts = opts
        self.batch_size  = self.opts.batch_size
        self.c_dim       = self.opts.c_dim
        self.output_size = self.opts.output_size
        self.z_dim       = self.opts.z_dim

        save_dir_str = 'o' + str(opts.output_size) + '_b' + str(opts.batch_size) + \
                        '_df' + str(opts.conv_dim) + '_epch' + str(opts.epoch)
        self.sample_dir = os.path.join(opts.sample_dir, opts.db_name, save_dir_str)
        self.test_dir = os.path.join(opts.test_dir, opts.db_name, save_dir_str)
        self.net_save_dir = os.path.join(opts.net_dir, opts.db_name, save_dir_str)
        if not os.path.exists(self.sample_dir):
            os.makedirs(self.sample_dir)
        if not os.path.exists(self.test_dir):
            os.makedirs(self.test_dir)
        if not os.path.exists(self.net_save_dir):
            os.makedirs(self.net_save_dir)


        if self.opts.use_gpu:
            self.Tensor = torch.cuda.FloatTensor
        else:
            self.Tensor = torch.Tensor

        transform_list = [transforms.ToTensor(),
                          transforms.Normalize((0.5, 0.5, 0.5),
                                               (0.5, 0.5, 0.5))]

        self.transform = transforms.Compose(transform_list)

        # input part1, part2, part3, images, gtMast, z
        self.input_image = Variable(self.Tensor(self.batch_size, self.c_dim, self.output_size, self.output_size))
        self.shuff_image = Variable(self.Tensor(self.batch_size, self.c_dim, self.output_size, self.output_size))
        self.input_part1  = Variable(self.Tensor(self.batch_size, self.c_dim, self.output_size, self.output_size))
        self.input_part2  = Variable(self.Tensor(self.batch_size, self.c_dim, self.output_size, self.output_size))
        self.input_part3  = Variable(self.Tensor(self.batch_size, self.c_dim, self.output_size, self.output_size))
        self.input_z      = Variable(self.Tensor(self.batch_size, self.z_dim, 1, 1))
        self.gt_mask      = Variable(self.Tensor(self.batch_size, 1, self.output_size, self.output_size))
        self.weight_g_loss = Variable(self.Tensor(1))

        # find depth of network
        num_conv_layers = 0
        osize = self.opts.output_size / 4
        while(True):
            osize = osize / 2
            if osize < 1:
                break
            num_conv_layers = num_conv_layers + 1
        self.opts.num_conv_layers = num_conv_layers

        # define net_discriminator
        self.net_discriminator  = Discriminator(self.opts)
        self.net_discriminator.apply(weights_init)
        # define net_generator
        self.net_generator      = ImageGenerator(self.opts)
        self.net_generator.apply(weights_init)
        # define net_part_encoder
        self.net_part_encoder   = PartEncoder(self.opts)
        self.net_part_encoder.apply(weights_init)
        # define net_part_decoder
        self.net_mask_generator = MaskGenerator(self.opts)
        self.net_mask_generator.apply(weights_init)

        if self.opts.cont_train:
            self.load(self.opts.start_epoch)

        if self.opts.use_gpu:
            # torch.cuda.set_device(self.opts.gpu_id)
            self.net_discriminator = nn.DataParallel(self.net_discriminator.cuda())
            self.net_generator = nn.DataParallel(self.net_generator.cuda())
            self.net_part_encoder = nn.DataParallel(self.net_part_encoder.cuda())
            self.net_mask_generator = nn.DataParallel(self.net_mask_generator.cuda())

        # define optimizer
        self.criterionMask = torch.nn.L1Loss(size_average=False)
        self.criterionAppr = torch.nn.L1Loss(size_average=False)
        self.criterionGAN = torch.nn.BCELoss()

        self.optimizer_G = torch.optim.Adam(itertools.chain(self.net_generator.parameters(),
                                                            self.net_part_encoder.parameters(),
                                                            self.net_mask_generator.parameters()),
                                            lr=self.opts.learning_rate,
                                            betas=(self.opts.beta1, 0.999))
        self.optimizer_D = torch.optim.Adam(self.net_discriminator.parameters(),
                                            lr=self.opts.learning_rate,
                                            betas=(self.opts.beta1, 0.999))

        self.vis = visdom.Visdom(port=self.opts.visdom_port)

    def forward(self):

        ''' Encoding Key parts '''
        self.part1_enc_out = self.net_part_encoder(self.input_part1)
        self.part2_enc_out = self.net_part_encoder(self.input_part2)
        self.part3_enc_out = self.net_part_encoder(self.input_part3)

        self.parts_enc = []
        for val in range(len(self.part1_enc_out)):
            self.parts_enc.append (self.part1_enc_out[val] + self.part2_enc_out[val] + self.part3_enc_out[val])

        ''' Generating mask'''
        self.gen_mask_output = self.net_mask_generator(self.parts_enc)
        self.gen_mask = self.gen_mask_output[-1]

        ''' Generating Full image'''
        self.image_gen_output = self.net_generator(self.parts_enc[-1], self.input_z,
                                               self.gen_mask_output)
        self.image_gen = self.image_gen_output[-1]

    def backward_D(self):
        # self.real_gtother = torch.mul(self.input_image, 1 - self.gt_mask)  # realother
        self.genpart_realbg = torch.mul(self.image_gen, self.gt_mask) + \
                              torch.mul(self.input_image, 1 - self.gt_mask)  # GR
        self.realpart_genbg = torch.mul(self.image_gen, 1 - self.gt_mask) + \
                              torch.mul(self.input_image, self.gt_mask)  # RG
        self.shfpart_realbg = torch.mul(self.shuff_image, self.gt_mask) + \
                              torch.mul(self.input_image, 1 - self.gt_mask)  # SR
        self.realpart_shfbg = torch.mul(self.input_image, self.gt_mask) + \
                              torch.mul(self.shuff_image, 1 - self.gt_mask)  # RS

        self.d_real = self.net_discriminator(self.input_image.detach())
        self.d_gen = self.net_discriminator(self.image_gen.detach())
        self.d_genpart_realbg = self.net_discriminator(self.genpart_realbg.detach())
        self.d_realpart_genbg = self.net_discriminator(self.realpart_genbg.detach())
        self.d_shfpart_realbg = self.net_discriminator(self.shfpart_realbg.detach())
        self.d_realpart_shfbg = self.net_discriminator(self.realpart_shfbg.detach())

        true_tensor = Variable(self.Tensor(self.d_real.data.size()).fill_(1.0))
        fake_tensor = Variable(self.Tensor(self.d_real.data.size()).fill_(0.0))
        self.d_loss = self.criterionGAN(self.d_real, true_tensor) + \
                      self.criterionGAN(self.d_gen, fake_tensor) + \
                      self.criterionGAN(self.d_shfpart_realbg, fake_tensor) + \
                      self.criterionGAN(self.d_realpart_shfbg, fake_tensor)
                      # 1/5 * (self.criterionGAN(self.d_gen, fake_tensor) +
                      #        self.criterionGAN(self.d_genpart_realbg, fake_tensor) +
                      #        self.criterionGAN(self.d_realpart_genbg, fake_tensor) +
                      #        self.criterionGAN(self.d_shfpart_realbg, fake_tensor) +
                      #        self.criterionGAN(self.d_realpart_shfbg, fake_tensor))
        self.d_loss.backward()

    def backward_G(self):
        self.d_real = self.net_discriminator(self.input_image)
        self.d_gen = self.net_discriminator(self.image_gen)
        self.real_gtpart = torch.mul(self.input_image, self.gt_mask)  # realpart
        self.gen_genpart = torch.mul(self.image_gen, self.gen_mask)  # genpart

        true_tensor = Variable(self.Tensor(self.d_real.size()).fill_(1.0))
        self.g_loss_l1_mask = self.criterionMask(self.gen_mask, self.gt_mask)
        self.g_loss_l1_appr = self.criterionAppr(self.gen_genpart, self.real_gtpart)
        self.g_loss_gan = self.criterionGAN(self.d_gen, true_tensor)
        self.g_loss = self.weight_g_loss1 * self.g_loss_l1_mask + \
                      self.weight_g_loss2 * self.g_loss_l1_appr + \
                      1.0 * self.g_loss_gan

        # tt = time.time()
        self.g_loss.backward()
        # print ('%f' % (time.time()-tt))

    def optimize_parameters_D(self):
        self.optimizer_D.zero_grad()
        self.backward_D()
        self.optimizer_D.step()

    def optimize_parameters_G(self):
        self.optimizer_G.zero_grad()
        self.backward_G()
        self.optimizer_G.step()

    def visualize(self, win_offset=0):

        # show input image
        # show gen image
        # show pred mask
        ups = nn.Upsample(scale_factor=2, mode='nearest')
        input_image = (self.input_image[0:8].cpu().data + 1.0) / 2.0
        image_gen   = (self.image_gen[0:8].cpu().data + 1.0) / 2.0
        gen_mask    = (self.gen_mask[0:8].cpu().data)
        gen_mask = gen_mask * image_gen
        gt_mask     = (self.gt_mask[0:8].cpu().data)
        gt_mask = gt_mask * input_image
        self.vis.images(input_image, win=win_offset+0, opts=dict(title='input images'))
        self.vis.images(image_gen,   win=win_offset+1, opts=dict(title='generated images'))
        self.vis.images(gen_mask,    win=win_offset+2, opts=dict(title='predicted masks'))
        self.vis.images(gt_mask,     win=win_offset+3, opts=dict(title='gt masks'))

    def save_images(self, epoch, iter, is_test=False):
        num_img_rows = 7
        num_img_cols = 16

        input_image = (self.input_image[0:num_img_cols].cpu().data + 1.0) / 2.0
        input_part1 = (self.input_part1[0:num_img_cols].cpu().data + 1.0) / 2.0
        input_part2 = (self.input_part2[0:num_img_cols].cpu().data + 1.0) / 2.0
        input_part3 = (self.input_part3[0:num_img_cols].cpu().data + 1.0) / 2.0
        image_gen   = (self.image_gen[0:num_img_cols].cpu().data + 1.0) / 2.0
        gen_mask    = (self.gen_mask[0:num_img_cols].cpu().data)
        gen_mask    = gen_mask * image_gen
        gt_mask     = (self.gt_mask[0:num_img_cols].cpu().data)
        gt_mask     = gt_mask * input_image

        input_image_pil = [transforms.ToPILImage()(input_image[i]) for i in range(input_image.shape[0])]
        input_part1_pil = [transforms.ToPILImage()(input_part1[i]) for i in range(input_part1.shape[0])]
        input_part2_pil = [transforms.ToPILImage()(input_part2[i]) for i in range(input_part2.shape[0])]
        input_part3_pil = [transforms.ToPILImage()(input_part3[i]) for i in range(input_part3.shape[0])]
        image_gen_pil   = [transforms.ToPILImage()(image_gen[i])   for i in range(image_gen.shape[0])]
        gen_mask_pil    = [transforms.ToPILImage()(gen_mask[i])    for i in range(gen_mask.shape[0])]
        gt_mask_pil     = [transforms.ToPILImage()(gt_mask[i])     for i in range(gt_mask.shape[0])]

        im_w = input_image.shape[2]
        im_h = input_image.shape[3]

        image_save = Image.new('RGB', (num_img_cols*im_w, num_img_rows*im_h))

        for i in range(num_img_cols):
            image_save.paste(input_part1_pil[i],   (im_w*i, im_h*0))
            image_save.paste(input_part2_pil[i],   (im_w*i, im_h*1))
            image_save.paste(input_part3_pil[i],   (im_w*i, im_h*2))
            image_save.paste(input_image_pil[i],   (im_w*i, im_h*3))
            image_save.paste(image_gen_pil[i],     (im_w*i, im_h*4))
            image_save.paste(gen_mask_pil[i],      (im_w*i, im_h*5))
            image_save.paste(gt_mask_pil[i],       (im_w*i, im_h*6))

        save_name = "epoch_%02d_iter_%04d.png" %(epoch, iter)
        if is_test:
            save_image_path = os.path.join(self.test_dir, save_name)
        else:
            save_image_path = os.path.join(self.sample_dir, save_name)

        image_save.save(save_image_path)


    def set_inputs_for_test(self, input_image, input_part1, input_part2, input_part3, z):
        self.input_image = Variable(self.Tensor(self.batch_size, self.c_dim, self.output_size, self.output_size))
        self.input_part1  = Variable(self.Tensor(self.batch_size, self.c_dim, self.output_size, self.output_size))
        self.input_part2  = Variable(self.Tensor(self.batch_size, self.c_dim, self.output_size, self.output_size))
        self.input_part3  = Variable(self.Tensor(self.batch_size, self.c_dim, self.output_size, self.output_size))
        self.input_z = Variable(z)

        # stack tensors
        for i in range(len(input_image)):
            self.input_image[i,:,:,:] = self.transform(input_image[i])
            self.input_part1[i,:,:,:] = self.transform(input_part1[i])
            self.input_part2[i,:,:,:] = self.transform(input_part2[i])
            self.input_part3[i,:,:,:] = self.transform(input_part3[i])

        if self.opts.use_gpu:
            self.input_image = self.input_image.cuda()
            self.input_part1 = self.input_part1.cuda()
            self.input_part2 = self.input_part2.cuda()
            self.input_part3 = self.input_part3.cuda()
            self.input_z     = self.input_z.cuda()


    def set_inputs_for_train(self, input_image, shuff_image, input_part1, input_part2, input_part3,
                   z, gt_mask, weight_g_loss1, weight_g_loss2):

        self.input_image   = Variable(self.Tensor(self.batch_size, self.c_dim, self.output_size, self.output_size))
        self.shuff_image   = Variable(self.Tensor(self.batch_size, self.c_dim, self.output_size, self.output_size))
        self.input_part1   = Variable(self.Tensor(self.batch_size, self.c_dim, self.output_size, self.output_size))
        self.input_part2   = Variable(self.Tensor(self.batch_size, self.c_dim, self.output_size, self.output_size))
        self.input_part3   = Variable(self.Tensor(self.batch_size, self.c_dim, self.output_size, self.output_size))
        self.gt_mask       = Variable(self.Tensor(self.batch_size, 1, self.output_size, self.output_size))
        self.input_z       = Variable(z)
        self.weight_g_loss1 = Variable(self.Tensor([weight_g_loss1]))
        self.weight_g_loss2 = Variable(self.Tensor([weight_g_loss2]))

        # stack tensors
        for i in range(len(input_image)):
            self.input_image[i,:,:,:] = self.transform(input_image[i])
            self.shuff_image[i,:,:,:] = self.transform(shuff_image[i])
            self.input_part1[i,:,:,:] = self.transform(input_part1[i])
            self.input_part2[i,:,:,:] = self.transform(input_part2[i])
            self.input_part3[i,:,:,:] = self.transform(input_part3[i])
            self.gt_mask[i,0,:,:] = gt_mask[i]

        if self.opts.use_gpu:
            self.input_image = self.input_image.cuda()
            self.shuff_image = self.shuff_image.cuda()
            self.input_part1 = self.input_part1.cuda()
            self.input_part2 = self.input_part2.cuda()
            self.input_part3 = self.input_part3.cuda()
            self.gt_mask    = self.gt_mask.cuda()
            self.input_z    = self.input_z.cuda()
            self.weight_g_loss = self.weight_g_loss.cuda()

    def save(self, epoch):
        self.save_network(self.net_discriminator, epoch, 'net_disc')
        self.save_network(self.net_generator, epoch, 'net_imggen')
        self.save_network(self.net_part_encoder, epoch, 'net_partenc')
        self.save_network(self.net_mask_generator, epoch, 'net_maskgen')

    def load(self, epoch):
        self.load_network(self.net_discriminator, epoch, 'net_disc')
        self.load_network(self.net_generator, epoch, 'net_imggen')
        self.load_network(self.net_part_encoder, epoch, 'net_partenc')
        self.load_network(self.net_mask_generator, epoch, 'net_maskgen')


    def save_network(self, network, epoch, net_name):
        save_filename = 'epoch_%s_net_%s.pth' % (epoch, net_name)
        save_path = os.path.join(self.net_save_dir, save_filename)
        torch.save(network.cpu().state_dict(), save_path)
        if self.opts.use_gpu:
            network.cuda()

    def load_network(self, network, epoch, net_name):
        save_filename = 'epoch_%s_net_%s.pth' % (epoch, net_name)
        save_path = os.path.join(self.net_save_dir, save_filename)
        network.load_state_dict(torch.load(save_path))



class PartEncoder(nn.Module):
    def __init__(self, opts):
        super(PartEncoder, self).__init__()

        self.opts = opts
        self.num_conv_layers = opts.num_conv_layers

        conv_dims_in = [self.opts.c_dim]
        conv_dims_out = []

        for i in range(self.opts.num_conv_layers):
            powers = min(3, i)
            conv_dims_in.append(opts.conv_dim * np.power(2,powers))
            conv_dims_out.append(opts.conv_dim * np.power(2,powers))
        conv_dims_out.append(self.opts.part_embed_dim)

        self.conv = []
        self.actv = []
        self.layer = []

        for i in range(self.opts.num_conv_layers + 1):
            if i == self.opts.num_conv_layers:
                _kernel_size = np.int(self.opts.output_size / np.power(2, self.opts.num_conv_layers))
                _stride = 1
                _padding = 0
            else:
                _kernel_size = 5
                _stride = 2
                _padding = 2

            if i == 0 or i == self.opts.num_conv_layers:
                self.actv.append(nn.LeakyReLU(0.2))
            else:
                self.actv.append(nn.Sequential(nn.BatchNorm2d(conv_dims_out[i]),nn.LeakyReLU(0.2)))

            self.conv.append(nn.Conv2d(conv_dims_in[i], conv_dims_out[i],
                                       kernel_size=_kernel_size, stride=_stride, padding=_padding, bias=True))
            self.layer.append(nn.Sequential(self.conv[i], self.actv[i]))

        model = [self.layer[i] for i in range(len(self.layer))]
        self.model = nn.Sequential(*model)



    def forward(self, x):

        self.e = []
        for i in range(self.opts.num_conv_layers+1):
            if i == 0 :
                self.e.append(self.actv[i](self.conv[i](x)))
            else:
                self.e.append(self.actv[i](self.conv[i](self.e[i-1])))
        return self.e


class MaskGenerator(nn.Module):
    def __init__(self, opts):
        super(MaskGenerator, self).__init__()

        self.opts = opts
        conv_dims_in = [opts.part_embed_dim]
        conv_dims_out = []

        for i in range(self.opts.num_conv_layers ):
            powers = min(3, self.opts.num_conv_layers - 1 - i)
            conv_dims_in.append(opts.conv_dim * np.power(2,powers) * 2)
            conv_dims_out.append(opts.conv_dim * np.power(2,powers))
        conv_dims_out.append(1)

        self.convT = []
        self.actv = []
        self.layer = []

        for i in range(self.opts.num_conv_layers + 1):
            if i == 0:
                _kernel_size = np.int(self.opts.output_size / np.power(2, self.opts.num_conv_layers))
                _stride = 1
                _padding = 0
            else:
                _kernel_size = 5
                _stride = 2
                _padding = 2

            if i == self.opts.num_conv_layers:
                self.actv.append(nn.Sigmoid())
            else:
                self.actv.append(nn.Sequential(nn.BatchNorm2d(conv_dims_out[i]), nn.ReLU()))

            if i == 0:
                self.convT.append(nn.Linear(conv_dims_in[i], conv_dims_out[i] * _kernel_size * _kernel_size ))
            else:
                self.convT.append(nn.ConvTranspose2d(conv_dims_in[i], conv_dims_out[i],
                                       kernel_size=_kernel_size, stride=_stride, padding=_padding, bias=True))

            self.layer.append(nn.Sequential(self.convT[i], self.actv[i]))

        model = [self.layer[i] for i in range(len(self.layer))]
        self.model = nn.Sequential(*model)


    def forward(self, parts_enc):

        len_parts_enc = len(parts_enc)
        _output_size = []
        for i in range(len_parts_enc):
            if i== 0:
                _output_size.append(4)
            else:
                _output_size.append(_output_size[i-1]*2)

        self.m = []

        for i in range(len_parts_enc):
            if i == 0 :
                # self.m.append(self.actv[i](self.convT[i](parts_enc[-1], output_size=[_output_size[i], _output_size[i]])))
                enc_dims = parts_enc[-1].size(1)*parts_enc[-1].size(2)*parts_enc[-1].size(3)
                self.m.append(self.convT[i](parts_enc[-1].view(-1,enc_dims)))
                num_chns = self.m[i].size(1) / (_output_size[i]*_output_size[i])
                self.m[i] = self.actv[i](self.m[i].view(-1,num_chns,_output_size[i],_output_size[i]))
                self.m[i] = torch.cat([self.m[i], parts_enc[-2 - i]], 1)
            elif i == (len_parts_enc - 1):
                self.m.append(self.actv[i](self.convT[i](self.m[i-1], output_size=[_output_size[i], _output_size[i]])))
            else:
                self.m.append(self.actv[i](self.convT[i](self.m[i-1], output_size=[_output_size[i], _output_size[i]])))
                self.m[i] = torch.cat([self.m[i], parts_enc[-2 - i]], 1)

        return self.m


class Discriminator(nn.Module):
    def __init__(self, opts):
        super(Discriminator, self).__init__()

        self.opts = opts
        self.num_conv_layers = opts.num_conv_layers

        conv_dims_in = [self.opts.c_dim]
        conv_dims_out = []

        for i in range(self.opts.num_conv_layers):
            powers = min(3, i)
            conv_dims_in.append(opts.conv_dim * np.power(2, powers))
            conv_dims_out.append(opts.conv_dim * np.power(2, powers))
        conv_dims_out.append(1)

        self.conv = []
        self.actv = []
        self.layer = []

        for i in range(self.opts.num_conv_layers + 1):

            if i == self.opts.num_conv_layers:
                _kernel_size = np.int(self.opts.output_size / np.power(2, self.opts.num_conv_layers))
                _stride = 1
                _padding = 0
            else:
                _kernel_size = 5
                _stride = 2
                _padding = 2

            if i == 0:
                self.actv.append(nn.LeakyReLU(0.2))
            elif i == self.opts.num_conv_layers:
                self.actv.append(nn.Sigmoid())
            else:
                self.actv.append(nn.Sequential(nn.BatchNorm2d(conv_dims_out[i]),nn.LeakyReLU(0.2)))

            self.conv.append(nn.Conv2d(conv_dims_in[i], conv_dims_out[i],
                                       kernel_size=_kernel_size, stride=_stride, padding=_padding, bias=True))
            self.layer.append(nn.Sequential(self.conv[i], self.actv[i]))

        model = [self.layer[i] for i in range(len(self.layer))]
        self.model = nn.Sequential(*model)


    def forward(self, x):

        self.d = []
        for i in range(len(self.conv)):
            if i == 0 :
                self.d.append(self.actv[i](self.conv[i](x)))
            else:
                self.d.append(self.actv[i](self.conv[i](self.d[i-1])))
        return self.d[-1]





class ImageGenerator(nn.Module):
    def __init__(self, opts):
        super(ImageGenerator, self).__init__()

        self.opts = opts
        conv_dims_in = [opts.part_embed_dim + opts.z_dim]
        conv_dims_out = []

        for i in range(self.opts.num_conv_layers):
            powers = min(3, self.opts.num_conv_layers - 1 - i)
            conv_dims_in.append(opts.conv_dim * np.power(2, powers) * 3)
            conv_dims_out.append(opts.conv_dim * np.power(2, powers))
        conv_dims_out.append(opts.c_dim)

        self.convT = []
        self.actv = []
        self.layer = []
        for i in range(self.opts.num_conv_layers + 1):
            if i == 0:
                _kernel_size = np.int(self.opts.output_size / np.power(2, self.opts.num_conv_layers))
                _stride = 1
                _padding = 0
            else:
                _kernel_size = 5
                _stride = 2
                _padding = 2

            if i == self.opts.num_conv_layers:
                self.actv.append(nn.Tanh())
            else:
                self.actv.append(nn.Sequential(nn.BatchNorm2d(conv_dims_out[i]), nn.ReLU()))

            if i == 0:
                self.convT.append(nn.Linear(conv_dims_in[i], conv_dims_out[i] * _kernel_size * _kernel_size ))
            else:
                self.convT.append(nn.ConvTranspose2d(conv_dims_in[i], conv_dims_out[i],
                                       kernel_size=_kernel_size, stride=_stride, padding=_padding, bias=True))

            # self.convT.append(nn.ConvTranspose2d(conv_dims_in[i], conv_dims_out[i],
            #                            kernel_size=_kernel_size, stride=_stride, padding=_padding, bias=False))
            self.layer.append(nn.Sequential(self.convT[i], self.actv[i]))

        model = [self.layer[i] for i in range(len(self.layer))]
        self.model = nn.Sequential(*model)


    def forward(self, embed, z, m):
        self.embed_z = torch.cat([embed, z], 1)
        self.g = []
        len_m = len(m)

        _output_size = []
        for i in range(len_m):
            if i== 0:
                _output_size.append(4)
            else:
                _output_size.append(_output_size[i-1]*2)

        self.g = []

        for i in range(len_m):
            if i == 0 :
                # self.g.append(self.actv[i](self.convT[i](self.embed_z, output_size=[_output_size[i], _output_size[i]])))
                enc_dims = self.embed_z.size(1) * self.embed_z.size(2) * self.embed_z.size(3)
                self.g.append(self.convT[i](self.embed_z.view(-1, enc_dims)))
                num_chns = self.g[i].size(1) / (_output_size[i] * _output_size[i])
                self.g[i] = self.actv[i](self.g[i].view(-1, num_chns, _output_size[i], _output_size[i]))

                self.g[i] = torch.cat([self.g[i], m[i]], 1)
            elif i == (len_m - 1):
                self.g.append(self.actv[i](self.convT[i](self.g[i-1], output_size=[_output_size[i], _output_size[i]])))
            else:
                self.g.append(self.actv[i](self.convT[i](self.g[i-1], output_size=[_output_size[i], _output_size[i]])))
                self.g[i] = torch.cat([self.g[i], m[i]], 1)

        return self.g

